# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
import config
from dateutil.parser import parse
from models import db, VirtualDesktopSessions
from utils.error import SocaError
from utils.response import SocaResponse
from utils.cast import SocaCastEngine
from models import db
import utils.aws.boto3_wrapper as utils_boto3
import time
from datetime import datetime, timedelta, timezone
import pytz
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import islice
from typing import Literal, Union
import math
import os

logger = logging.getLogger("scheduled_tasks_virtual_desktops_schedule_management")

client_ec2 = utils_boto3.get_boto(service_name="ec2").message
client_ssm = utils_boto3.get_boto(service_name="ssm").message
client_cloudformation = utils_boto3.get_boto(service_name="cloudformation").message


def ssm_get_command_info(
    os_family: Literal["linux", "windows"]
) -> Union[SocaResponse, SocaError]:
    """
    Returns the SSM command & document name to run based on the operating system
    """

    if os_family not in ["linux", "windows"]:
        return SocaError.GENERIC_ERROR(
            success=False,
            message=f"os_family must be linux or windows, detected {os_family}",
        )

    if os_family == "windows":
        _ssm_commands = [
            f"$DCV_Describe_Session = Invoke-Expression \"& 'C:\\Program Files\\NICE\\DCV\\Server\\bin\\dcv' describe-session $env:SOCA_DCV_SESSION_ID -j\" | ConvertFrom-Json",
            '$CPUAveragePerformanceLast10Secs = (GET-COUNTER -Counter "\\Processor(_Total)\\% Processor Time" -SampleInterval 2 -MaxSamples 5 |select -ExpandProperty countersamples | select -ExpandProperty cookedvalue | Measure-Object -Average).average',
            "$output = @{}",
            '$output["CPUAveragePerformanceLast10Secs"] = $CPUAveragePerformanceLast10Secs',
            '$output["DCVCurrentConnections"] = $DCV_Describe_Session."num-of-connections"',
            '$output["DCVCreationTime"] = $DCV_Describe_Session."creation-time"',
            '$output["DCVLastDisconnectTime"] = $DCV_Describe_Session."last-disconnection-time"',
            "$output | ConvertTo-Json",
        ]
        _ssm_document_name = "AWS-RunPowerShellScript"

    else:
        _ssm_commands = [
            "export SOCA_DCV_SESSION_ID=$(cat /etc/environment | grep SOCA_DCV_SESSION_ID= | awk -F'=' '{print $2}')",  # ssm.send_command() cannot use source",
            "DCV_Describe_Session=$(dcv describe-session $SOCA_DCV_SESSION_ID -j)",
            'echo "${DCV_Describe_Session}" | jq --arg CPUAveragePerformanceLast10Secs "$(top -d 5 -b -n2 | grep \'Cpu(s)\' | tail -n 1 | awk \'{print $2 + $4}\')" \'{"DCVCurrentConnections": .["num-of-connections"], "DCVCreationTime": .["creation-time"], "DCVLastDisconnectTime": .["last-disconnection-time"], "CPUAveragePerformanceLast10Secs": $CPUAveragePerformanceLast10Secs }\'',
        ]
        _ssm_document_name = "AWS-RunShellScript"

    return SocaResponse(
        success=True,
        message={
            "ssm_commands": _ssm_commands,
            "ssm_document_name": _ssm_document_name,
        },
    )


def ssm_get_list_command_status(command_id: str) -> Union[SocaResponse, SocaError]:
    """
    Returns the status of the SSM command ID.
    Valid status are either Success or Failed (this means the SSM command has completed successfully)
    All other SSM status code will return a SocaError
    """
    _max_ssm_loop_attempts = 10
    _ssm_attempt = 1
    while True:
        _check_command_status = client_ssm.list_commands(CommandId=command_id)[
            "Commands"
        ][0]["Status"]
        logger.info(f"Status command for {command_id}: {_check_command_status}")
        if _check_command_status in ["Success", "Failed"]:
            return SocaResponse(
                success=True,
                message=f"Command {command_id} has completed, checking each instance results",
            )
        else:
            if _check_command_status in ["InProgress", "Pending"]:
                if _ssm_attempt == _max_ssm_loop_attempts:
                    return SocaError.GENERIC_ERROR(
                        helper=f"Unable to determine status SSM responses after timeout for {command_id}"
                    )
                else:
                    time.sleep(5)
                    _ssm_attempt += 1
            else:
                return SocaError.GENERIC_ERROR(
                    helper=f"SSM command {command_id} exited with invalid status {_check_command_status=}"
                )


def start_instances(
    sessions_info: list[VirtualDesktopSessions],
) -> None:
    """
    Start EC2 instances
    Important, start_instances and stop_instances can take up to 50 Instance IDs. Make sure session_info chunk size is max 50.
    """
    if not isinstance(sessions_info, list):
        logger.critical(
            f"Unable to start instances, sessions_info must be a list of VirtualDesktopSessions objects"
        )
        return

    logger.info(f"Starting instances: {sessions_info}")
    _successful_sessions = sessions_info
    try:
        client_ec2.start_instances(
            InstanceIds=[session.instance_id for session in sessions_info]
        )

    except Exception as err:
        logger.warning(
            f"Unable to start instance from this chunk {sessions_info} due to {err}, trying to proceed one by one"
        )
        for _session in sessions_info:
            try:
                client_ec2.start_instances(InstanceIds=[_session.instance_id])
            except Exception as err:
                logger.error(
                    f"Unable to start instance {_session.instance_id} due to {err}"
                )
                _successful_sessions.remove(_session)

    for _session in _successful_sessions:
        try:
            _session.session_state = "pending"
            _session.session_state_latest_change_time = datetime.now(timezone.utc)
            db.session.commit()
            logger.info(f"Started {_session} successfully {_session.instance_id=}")
        except Exception as err:
            logger.error(
                f"{_session.instance_id} from {_session} was started successfully but unable to update DB entry due to {err}, updating it back to stopped"
            )
            try:
                client_ec2.stop_instances(InstanceIds=[_session.instance_id])
            except Exception as err:
                logger.error(
                    f"Unable to stop {_session} instance {_session.instance_id} due to {err}"
                )


def find_inactive_sessions(sessions_info: list[VirtualDesktopSessions]) -> None:
    """
    Identify Linux or Windows instances that should be stopped and execute an SSM command to check for any ongoing activity.
    """
    _windows_sessions_instance_ids = [
        session.instance_id
        for session in sessions_info
        if session.os_family == "windows"
    ]

    _linux_sessions_instance_ids = [
        session.instance_id for session in sessions_info if session.os_family == "linux"
    ]

    if (
        get_idle_time_windows := SocaCastEngine(
            data=config.Config.DCV_WINDOWS_STOP_IDLE_SESSION
        ).cast_as(int)
    ).get("success") is True:
        _stop_instance_after_idle_time_windows = get_idle_time_windows.get("message")
    else:
        logger.critical(
            "DCV_WINDOWS_STOP_IDLE_SESSION does not seems to be a valid integer"
        )
        return

    if (
        get_idle_time_linux := SocaCastEngine(
            data=config.Config.DCV_LINUX_STOP_IDLE_SESSION
        ).cast_as(int)
    ).get("success") is True:
        _stop_instance_after_idle_time_linux = get_idle_time_linux.get("message")
    else:
        logger.critical(
            "DCV_LINUX_STOP_IDLE_SESSION does not seems to be a valid integer"
        )
        return

    # Do not run checks unless all SSM commands succeeded
    _skip_linux = True
    _skip_windows = True

    # Validate Linux SSM
    if _linux_sessions_instance_ids:
        logger.info(
            f"Detected the following Linux VDI running on {_linux_sessions_instance_ids=}"
        )
        _linux_ssm_info = ssm_get_command_info(os_family="linux")
        if _linux_ssm_info.get("success") is False:
            logger.critical(
                f"Unable to retrieve SSM command info for linux due to {_linux_ssm_info.get('message')}"
            )
        else:
            _check_dcv_session_linux = client_ssm.send_command(
                InstanceIds=_linux_sessions_instance_ids,
                DocumentName=_linux_ssm_info.get("message").get("ssm_document_name"),
                Parameters={
                    "commands": _linux_ssm_info.get("message").get("ssm_commands")
                },
                TimeoutSeconds=30,
            )
            _ssm_command_id_linux = _check_dcv_session_linux["Command"]["CommandId"]
            if (
                ssm_get_list_command_status(command_id=_ssm_command_id_linux).get(
                    "success"
                )
                is False
            ):
                logger.error(
                    f"Unable to determine status SSM responses for linux instances {_ssm_command_id_linux=}"
                )
            else:
                logger.info("Running SSM Command on Linux hosts succeeded")
                _skip_linux = False
    else:
        logger.info("No Linux instances to check for schedule")
    # Validate Windows SSM
    if _windows_sessions_instance_ids:
        logger.info(
            f"Detected the following Windows VDI running on {_windows_sessions_instance_ids=}"
        )
        _windows_ssm_info = ssm_get_command_info(os_family="windows")
        if _windows_ssm_info.get("success") is False:
            logger.critical(
                f"Unable to retrieve SSM command info for Windows due to {_windows_ssm_info.get('message')}"
            )
        else:
            _check_dcv_session_windows = client_ssm.send_command(
                InstanceIds=_windows_sessions_instance_ids,
                DocumentName=_windows_ssm_info.get("message").get("ssm_document_name"),
                Parameters={
                    "commands": _windows_ssm_info.get("message").get("ssm_commands")
                },
                TimeoutSeconds=30,
            )
            _ssm_command_id_windows = _check_dcv_session_windows["Command"]["CommandId"]
            if (
                ssm_get_list_command_status(command_id=_ssm_command_id_windows).get(
                    "success"
                )
                is False
            ):
                logger.error(
                    f"Unable to determine status SSM responses for windows instances {_ssm_command_id_windows=}"
                )
            else:
                logger.info("Running SSM Command on Windows hosts succeeded")
                _skip_windows = False
    else:
        logger.info("No Windows instances to check for schedule")
    # Wait until the Commands have completed.
    # Succeed => All instances succeeded
    # Failed => At least 1 instance failed, but other may have succeeded
    # All others return code => SSM command was not executed for various reason (Quota, Rate Exceeded etc ..)
    if _skip_linux is False:
        for _session in [
            session for session in sessions_info if session.os_family == "linux"
        ]:
            stop_instance_if_inactive(
                ssm_command_id=_ssm_command_id_linux,
                stop_instance_after_idle_time=_stop_instance_after_idle_time_linux,
                session=_session,
            )

    # Check all Windows hosts individually
    if _skip_windows is False:
        for _session in [
            session for session in sessions_info if session.os_family == "windows"
        ]:
            stop_instance_if_inactive(
                ssm_command_id=_ssm_command_id_windows,
                stop_instance_after_idle_time=_stop_instance_after_idle_time_windows,
                session=_session,
            )


def stop_instance_if_inactive(
    ssm_command_id: str,
    stop_instance_after_idle_time: int,
    session: VirtualDesktopSessions,
) -> Union[SocaResponse, SocaError]:
    """
    Check if the instance is inactive and can be stopped, update the associated VirtualDesktopSessions if needed
    """

    _session_id = session.id
    _instance_id = session.instance_id
    _session_uuid = session.session_uuid
    _hibernate = session.support_hibernation
    _ssm_output = client_ssm.get_command_invocation(
        CommandId=ssm_command_id, InstanceId=_instance_id
    )
    _status = _ssm_output.get("Status")
    logger.info(
        f"Checking if {_instance_id=} is inactive and can be stopped for DCV Session {_session_id=} : {_ssm_output}"
    )

    if _status == "Success":
        logger.info(
            f"SSM output for {_instance_id} succeeded, checking current DCV & CPU usage"
        )
        _dcv_info = json.loads(_ssm_output.get("StandardOutputContent"))
        session_current_connection = int(_dcv_info["DCVCurrentConnections"])
        _session_cpu_average = float(_dcv_info["CPUAveragePerformanceLast10Secs"])

        if _dcv_info["DCVLastDisconnectTime"] == "":
            # handle case where user launched DCV but never accessed it
            last_dcv_disconnect = parse(_dcv_info["DCVCreationTime"])
        else:
            last_dcv_disconnect = parse(_dcv_info["DCVLastDisconnectTime"])

        logger.info(
            f"DCV Activity Info for instance ID {_instance_id}: {session_current_connection=} {session_current_connection=}. {_session_cpu_average=}. {last_dcv_disconnect=}. Desktop UUID {_session_uuid}"
        )

        if _session_cpu_average < config.Config.DCV_IDLE_CPU_THRESHOLD:
            if session_current_connection == 0:
                current_time = parse(
                    datetime.now()
                    .replace(microsecond=0)
                    .replace(tzinfo=timezone.utc)
                    .isoformat()
                )
                if (
                    last_dcv_disconnect + timedelta(hours=stop_instance_after_idle_time)
                ) < current_time:

                    logger.info(
                        f"{_instance_id} is ready to be stopped/hibernated {_hibernate=}, last access time {last_dcv_disconnect}, stop after idle time (hours): {stop_instance_after_idle_time}, current time is {current_time}"
                    )
                    try:
                        client_ec2.stop_instances(
                            InstanceIds=[_instance_id],
                            Hibernate=_hibernate,
                        )
                    except Exception as err:
                        logger.critical(
                            f"Unable to stop/hibernate instance {_instance_id=} due to {err} {_hibernate=}. Desktop UUID {_session_uuid}"
                        )

                    try:
                        session.session_state = "stopped"
                        session.session_state_latest_change_time = datetime.now(
                            timezone.utc
                        )
                        db.session.commit()
                        logger.info(f"{session} stopped successfully")

                    except Exception as err:
                        logger.error(
                            f"Unable to update DB entry for {_instance_id=} due to {err}. Desktop UUID {_session_uuid}"
                        )
                        try:
                            client_ec2.start_instances(
                                InstanceIds=[session.instance_id]
                            )
                        except Exception as err:
                            logger.error(
                                f"Unable to start {session} instance {session.instance_id} due to {err}"
                            )

                else:
                    logger.info(
                        f"{_instance_id=} NOT ready to be stopped/hibernated, last access time {last_dcv_disconnect}, stop after idle time (hours): {stop_instance_after_idle_time}, current time is {current_time} Desktop UUID {_session_uuid}"
                    )
            else:
                logger.info(
                    f"{_instance_id=} currently has active DCV sessions: {session_current_connection}. Desktop UUID {_session_uuid}"
                )

        else:
            logger.info(
                f"{_instance_id=} CPU usage {_session_cpu_average} is above threshold {config.Config.DCV_IDLE_CPU_THRESHOLD} so this host won't be subject to stop/hibernate. Desktop UUID {_session_uuid}"
            )

    else:
        logger.error(
            f"SSM command {ssm_command_id} on {_instance_id=} failed with error: {_ssm_output.get('StandardErrorContent')}"
        )


def process_chunk(vdi_sessions: list[VirtualDesktopSessions]):
    logger.info(f"Processing chunk: {vdi_sessions}")
    # Grace Period
    # - Will not stop a desktop if it was started within the grace period
    # - Will not start a desktop if it was stopped within  the grace period
    # In other word, even if your schedule is stopped all day, but you manually start your desktop, it will stays up and running for 1 hour)
    _grace_period = config.Config.DCV_SCHEDULE_GRACE_PERIOD_IN_HOURS

    try:
        _tz = pytz.timezone(config.Config.TIMEZONE)
    except pytz.exceptions.UnknownTimeZoneError:
        logger.error(
            f"Timezone {config.Config.TIMEZONE} configured by the admin does not exist. Defaulting to UTC. Refer to https://en.wikipedia.org/wiki/List_of_tz_database_time_zones for a full list of supported timezones"
        )
        _tz = pytz.timezone("UTC")

    _now = datetime.now(_tz)
    _day = _now.strftime("%A").lower()
    _now_in_minutes = _now.hour * 60 + _now.minute

    # Filter the sessions where _now is greater than or equal to session_state_latest_change_time + grace period
    _sessions_outside_of_grace_period = [
        session
        for session in vdi_sessions
        if _now
        >= _tz.localize(session.session_state_latest_change_time)
        + timedelta(hours=_grace_period)
    ]

    logger.info(
        f"List of VDI outside of Grace Period: {_sessions_outside_of_grace_period}"
    )
    if _sessions_outside_of_grace_period:
        logger.info(f"Today is {_day=}, {_now_in_minutes=}, {_now=}")

        # Starting instance is instant, so we begin with them
        logger.info(
            f"Checking sessions supposed to be started all-day but not running, starting them (if any)."
        )
        _sessions_running_all_day = [
            session
            for session in _sessions_outside_of_grace_period
            if json.loads(session.schedule).get(_day).get("stop") == 1440
            and json.loads(session.schedule).get(_day).get("start") == 1440
            and session.session_state != "running"
        ]
        if _sessions_running_all_day:
            logger.info(f"List of Sessions: {_sessions_running_all_day=}")
            start_instances(sessions_info=_sessions_running_all_day)
        else:
            logger.info("No Sessions found")

        logger.info(
            f"Checking sessions supposed to be running at this time but state is not running, starting them (if any)."
        )
        _sessions_schedule_start = [
            session
            for session in _sessions_outside_of_grace_period
            if json.loads(session.schedule).get(_day).get("start")
            < _now_in_minutes
            < json.loads(session.schedule).get(_day).get("stop")
            and session.session_state != "running"
        ]

        if _sessions_schedule_start:
            logger.info(f"List of Sessions: {_sessions_schedule_start=}")
            start_instances(sessions_info=_sessions_schedule_start)
        else:
            logger.info("No Sessions found")

        # Stopping session take a little longer as we need to compute the current CPU percentage on each machine, so we move them at the end
        logger.info(
            f"Checking sessions supposed to be stopped all-day but currently running, stopping them if inactive (if any)"
        )
        _sessions_stopped_all_day = [
            session
            for session in _sessions_outside_of_grace_period
            if json.loads(session.schedule).get(_day).get("stop") == 0
            and json.loads(session.schedule).get(_day).get("start") == 0
            and session.session_state == "running"
        ]
        if _sessions_stopped_all_day:
            logger.info(f"List of Sessions: {_sessions_stopped_all_day=}")
            find_inactive_sessions(sessions_info=_sessions_stopped_all_day)
        else:
            logger.info("No Sessions found")

        logger.info(
            f"Checking sessions supposed to be stopped at this time but state is running, stopping them if inactive (if any)"
        )
        _sessions_schedule_stop = [
            session
            for session in _sessions_outside_of_grace_period
            if _now_in_minutes > json.loads(session.schedule).get(_day).get("stop")
            and session.session_state == "running"
        ]
        if _sessions_schedule_stop:
            logger.info(f"List of Sessions: {_sessions_schedule_stop=}")
            find_inactive_sessions(sessions_info=_sessions_schedule_stop)
        else:
            logger.info("No Sessions found")
    else:
        logger.info(
            "No VDI info subject to Schedule Update as they are all within grace time"
        )


def chunked_iterable(iterable: VirtualDesktopSessions, chunk_size: int):
    """Utility function to create chunks of the iterable using islice."""
    # Iterate over the iterable and yield chunks of the specified size
    iterator = iter(iterable)
    for first in iterator:
        yield [first] + list(islice(iterator, chunk_size - 1))


def virtual_desktops_schedule_management():
    logger.info("Scheduled Task: virtual_desktops_schedule_management")

    _start_time = time.time()

    # Get all current active VDI
    _all_dcv_sessions = VirtualDesktopSessions.query.filter(
        VirtualDesktopSessions.is_active.is_(True)
    ).all()
    if _all_dcv_sessions:
        # Start by creating chunk of 50 VDI sessions maximum (this is the max number of InstanceIds we can pass to some boto3 API call)
        # Keep this limit below 50.
        _chunk_size = 50

        _chunks_of_sessions = chunked_iterable(_all_dcv_sessions, _chunk_size)

        for _chunk in _chunks_of_sessions:
            process_chunk(_chunk)

    else:
        logger.info("No active virtual desktops found")

    _end_time = time.time()
    logger.info(
        f"Scheduled task completed in {_end_time - _start_time:.2f} seconds for {len(_all_dcv_sessions)} sessions"
    )


def auto_terminate_stopped_instance():
    logger.info("Scheduled Task: auto_terminate_stopped_instance")
    try:
        _terminate_stopped_windows_instance_after = int(
            config.Config.DCV_WINDOWS_TERMINATE_STOPPED_SESSION
        )
        _terminate_stopped_linux_instance_after = int(
            config.Config.DCV_LINUX_TERMINATE_STOPPED_SESSION
        )
    except ValueError as err:
        return SocaError.GENERIC_ERROR(
            helper=f"_terminate_stopped_instance_after does not seems to be a valid integer Script will not proceed to auto-termination. Error: {err}, DCV_WINDOWS_TERMINATE_STOPPED_SESSION={config.Config.DCV_WINDOWS_TERMINATE_STOPPED_SESSION}, DCV_LINUX_TERMINATE_STOPPED_SESSION={config.Config.DCV_LINUX_TERMINATE_STOPPED_SESSION} ."
        )

    _all_stopped_dcv_sessions = []
    if _terminate_stopped_windows_instance_after > 0:
        logger.info(
            f"Windows instance will be terminated after {_terminate_stopped_windows_instance_after} hours"
        )
        _all_stopped_windows_dcv_sessions = VirtualDesktopSessions.query.filter(
            VirtualDesktopSessions.is_active == True,
            VirtualDesktopSessions.session_state == "stopped",
            VirtualDesktopSessions.os_family == "windows",
        ).all()
        _all_stopped_dcv_sessions.extend(_all_stopped_windows_dcv_sessions)

    if _terminate_stopped_linux_instance_after > 0:
        logger.info(
            f"Linux instance will be terminated after {_terminate_stopped_windows_instance_after} hours"
        )
        _all_stopped_linux_dcv_sessions = VirtualDesktopSessions.query.filter(
            VirtualDesktopSessions.is_active == True,
            VirtualDesktopSessions.session_state == "stopped",
            VirtualDesktopSessions.os_family == "linux",
        ).all()
        _all_stopped_dcv_sessions.extend(_all_stopped_linux_dcv_sessions)

    if _all_stopped_dcv_sessions:
        for session_info in _all_stopped_dcv_sessions:
            logger.info(
                f"Checking stopped session {session_info.session_name} owned by {session_info.session_owner}"
            )

            _stack_name = session_info.stack_name
            _os_family = session_info.os_family
            _session_state_latest_change_time = (
                session_info.session_state_latest_change_time
            )

            if _os_family == "windows":
                _terminate_stopped_instance_after = (
                    _terminate_stopped_windows_instance_after
                )
            else:
                _terminate_stopped_instance_after = (
                    _terminate_stopped_linux_instance_after
                )

            if _terminate_stopped_instance_after == 0:
                logger.info("_terminate_stopped_instance_after is disabled, skipping")
                continue

            if (
                _session_state_latest_change_time
                + timedelta(hours=_terminate_stopped_instance_after)
            ) < datetime.now(timezone.utc):
                logger.info(
                    f"Desktop {session_info.session_uuid} is ready to be terminated, last access time {_session_state_latest_change_time}, stop after idle time (hours): {_terminate_stopped_instance_after}"
                )
                try:
                    client_cloudformation.delete_stack(StackName=_stack_name)
                except Exception as err:
                    return SocaError.GENERIC_ERROR(
                        helper=f"Unable to terminate instance {_stack_name} due to {err}"
                    )

                try:
                    session_info.is_active = False
                    session_info.deactivated_on = datetime.now(timezone.utc)
                    session_info.deactivated_by = "auto_terminate_stopped_instance"
                    session_info.session_state_latest_change_time = datetime.now(
                        timezone.utc
                    )
                    db.session.commit()
                    return SocaResponse(
                        success=True,
                        message=f"Terminated {_stack_name} successfully",
                    )
                except Exception as err:
                    return SocaError.GENERIC_ERROR(
                        helper=f"Unable to update DB entry for {_stack_name} due to {err}"
                    )
    else:
        logger.info(
            f"No stopped sessions found or feature is disabled (0). {config.Config.DCV_WINDOWS_TERMINATE_STOPPED_SESSION=} {config.Config.DCV_LINUX_TERMINATE_STOPPED_SESSION=}"
        )
