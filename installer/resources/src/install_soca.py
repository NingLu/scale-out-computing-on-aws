#!/usr/bin/env python3

######################################################################################################################
#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.                                                #
#                                                                                                                    #
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance    #
#  with the License. A copy of the License is located at                                                             #
#                                                                                                                    #
#      http://www.apache.org/licenses/LICENSE-2.0                                                                    #
#                                                                                                                    #
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES #
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions    #
#  and limitations under the License.                                                                                #
######################################################################################################################

"""
It's recommended to trigger this script via ./soca_installer.sh as python's virtual env and all required
libraries/dependencies will be automatically installed.

If you trigger ./install_soca.py directly, make sure to have all the Python and CDK dependencies installed
"""

import sys
# import re
import boto3
from collections import defaultdict
import botocore.exceptions
from requests import get
from requests.exceptions import RequestException, Timeout, ConnectionError
from botocore.client import ClientError
from botocore.exceptions import ProfileNotFound, ValidationError
from botocore import config
import shutil
import urllib3
import base64
import yaml
import json
import ast
from yaml.scanner import ScannerError
from types import SimpleNamespace
from rich import print
from rich.align import Align
from rich.console import Console
from rich.text import Text
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
)
import time
import datetime
import os
import re
import argparse
from shutil import make_archive
import logging
from rich.logging import RichHandler
import subprocess
import shlex

class CustomFormatter(logging.Formatter):
    def format(self, record):
        if not isinstance(record.msg, (Text, Table)):
            if record.levelno == logging.ERROR:
                record.msg = f"[bold red]ERROR: {record.msg}[/bold red]"
            elif record.levelno == logging.WARNING:
                record.msg = f"[bold yellow]WARNING: {record.msg} [/bold yellow]"

        return super().format(record)


_soca_debug = os.environ.get("SOCA_DEBUG", False)
if _soca_debug in ["1", "enabled", "true", "True", "on", "2", "trace"]:
    _log_level = logging.DEBUG
    _formatter = CustomFormatter("[%(asctime)s] %(levelname)s - %(message)s")
else:
    _log_level = logging.INFO
    _formatter = CustomFormatter("%(message)s")

_rich_handler = RichHandler(
    rich_tracebacks=True, markup=True, show_time=False, show_level=False
)
_rich_handler.setFormatter(_formatter)
logging.basicConfig(
    level=_log_level,
    handlers=[_rich_handler],
)

logger = logging.getLogger("soca_logger")

for _logger_name in ["boto3", "botocore"]:
    logging.getLogger(_logger_name).setLevel(
        logging.DEBUG if _soca_debug in {"trace", "2"} else logging.WARNING
    )


installer_path = "/".join(os.path.dirname(os.path.abspath(__file__)).split("/")[:-3])
sys.path.append(installer_path)
from installer.resources.src.prompt import get_input as get_input
from installer.resources.src.find_existing_resources import FindExistingResource

urllib3.disable_warnings()


def kms_prepare_account_aliases() -> int:
    """
    Query KMS for existing key AWS service aliases and create them if they don't exist.
    """
    logger.debug("Preparing KMS account aliases")
    _aliases_created: int = 0
    _aliases_existing: int = 0

    try:
        # Get all existing aliases
        _kms_paginator = kms.get_paginator("list_aliases")
        _kms_iterator = _kms_paginator.paginate()
        for _kms_aliases in _kms_iterator:
            for _alias in _kms_aliases.get("Aliases", []):

                _a_name: str = _alias.get("AliasName", "")

                # We are only concerned about AWS namespace alias that act as defaults for services
                if not _a_name.startswith("alias/aws/"):
                    logger.debug(
                        f"Ignoring KMS alias as it is not related to a service: {_a_name}"
                    )
                    continue

                # IndexError potential
                _a_servicename: str = _a_name.split("/")[-1]

                _a_arn: str = _alias.get("AliasArn", "")
                _a_create: datetime.datetime = _alias.get("CreationDate")
                _a_update: datetime.datetime = _alias.get("LastUpdatedDate")
                _a_key_id: str = _alias.get("TargetKeyId", "")

                if _a_create is None:
                    logger.info(
                        f"KMS - Creating first-time service alias for {_a_servicename} ({_a_name})"
                    )
                    kms.describe_key(KeyId=_a_name)
                    _aliases_created += 1
                else:
                    logger.debug(
                        f"KMS Alias for service {_a_servicename} ({_a_name}) already exists"
                    )
                    _aliases_existing += 1
                continue

    except ClientError as _err:
        logger.error(f"Unable to create KMS service alias: {_err}")
        return -1
    except Exception as _err:
        logger.error(f"Unable to create KMS service alias: {_err}")
        return -1

    # For the list of services that do not appear in the list_aliases until they are created
    for _alias_name in ["alias/aws/sns"]:
        logger.info(f"KMS - Checking service alias for {_alias_name}")
        _key = kms.describe_key(KeyId=_alias_name).get("KeyMetadata", {})
        if not _key:
            logger.error(f"Unable to lookup KMS service alias: {_alias_name}")
            sys.exit(1)
        _key_id: str = _key.get("KeyId", "")
        _key_create = _key.get("CreationDate", None)
        _key_manager: str = _key.get("KeyManager", "")
        if not _key_id:
            logger.error(f"Unable to create KMS service alias: {_alias_name}")
            sys.exit(1)

        logger.debug(
            f"Service default KMS key: {_alias_name}: {_key_id} / {_key_manager=}"
        )

    logger.debug(
        f"KMS service aliases created: {_aliases_created} / Existing: {_aliases_existing}"
    )
    return _aliases_created


def retrieve_secret_value(secret_id: str) -> dict:
    logger.debug(f"Fetching Secret ID - {secret_id}")
    _get_secret = secretsmanager.get_secret_value(SecretId=secret_id).get(
        "SecretString", None
    )
    if _get_secret:
        return ast.literal_eval(_get_secret)
    else:
        logger.error(f"Unable to fetch secret {secret_id}")
        return {}


def format_byte_size(num, suffix="B"):
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"


def get_install_properties(pathname: str) -> dict:
    # Retrieve SOCA configuration properties
    logger.debug(f"Configuration file path: {pathname}")
    try:
        with open(pathname, "r") as config_file:
            config_parameters = yaml.safe_load(config_file)
    except ScannerError as _err:
        logger.error(f"{pathname} is not a valid YAML file. Verify syntax, {_err}")
        sys.exit(1)
    except FileNotFoundError:
        logger.error(
            f"{pathname} not found. Make sure the file exist and the path is correct."
        )
        sys.exit(1)

    if config_parameters:
        return config_parameters
    else:
        return {}
        # sys.exit("No parameters were found in configuration file.")


def detect_customer_ip() -> str:
    """
    Try to determine the customer IP address by using the checkip.amazonaws.com service.
    """

    check_url = "https://checkip.amazonaws.com/"

    logger.info(
        f"\n====== Trying to detect your IPv4 address via {check_url} . Use --client-ip to specify it manually if needed ======\n"
    )

    client_ip: str = ""
    try:
        get_client_ip = get(url=check_url, timeout=15)
        if get_client_ip.status_code == 200:
            # Should return a clean string. May still need sanity check
            client_ip = f"{str(get_client_ip.text).strip()}/32"

        else:
            logger.warning(
                f"Unable to automatically determine client IP via {check_url} . Error: {get_client_ip}"
            )

    except RequestException as _e:
        logger.warning(
            f"Unable to automatically determine client IP via {check_url} . Error: {_e}"
        )

    return client_ip


def build_lambda_dependency(install_directory: str):
    logger.info("Building Lambda dependency")
    lambda_functions_folders = f"{install_directory}/../functions/"
    for _dir in os.scandir(lambda_functions_folders):
        if _dir.is_file():
            continue
        for filename in os.listdir(_dir):
            if filename == "requirements.txt":
                logger.info(f"Installing Python dependencies for {_dir.path}")
                _cmd = [
                    "pip3", "install", "--python-version", "3.12",
                    "-r", f"{_dir.path}/requirements.txt",
                    "--platform", "manylinux2014_x86_64",
                    "--target", f"{_dir.path}",
                    "--implementation", "cp",
                    "--only-binary=:all:", "--upgrade"
                ]
                result = subprocess.run(_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode != 0:
                    logger.error(f"Error during Lambda Dependency")
                    sys.exit(1)


def upload_objects(install_directory: str, bucket: str, cluster_id: str):
    # Upload required assets to customer S3 bucket
    logger.info(f"\n====== Uploading install files to {bucket}/{cluster_id} ======\n")
    dist_directory = f"{install_directory}/../../dist/{cluster_id}/"
    if os.path.isdir(dist_directory):
        logger.info(
            f"{dist_directory} already exist. Creating a new one for your build"
        )
        shutil.rmtree(dist_directory)
    os.makedirs(dist_directory)

    # Move required file to dist/ directory
    make_archive(
        f"{dist_directory}soca", "gztar", f"{install_directory}/../../../source/soca"
    )

    for item in os.listdir(f"{install_directory}/../upload_to_s3/"):
        # Construct full path to item
        s = os.path.join(f"{install_directory}/../upload_to_s3/", item)
        d = os.path.join(f"{dist_directory}/config/do_not_delete/", item)

        # Move each item to the destination
        if os.path.isdir(s):
            shutil.move(s, d)
        else:
            shutil.move(s, d)

    try:
        shutil.rmtree(f"{install_directory}/../upload_to_s3/")
    except Exception as _e:
        print(f"Unable to delete {install_directory}/../upload_to_s3/ because of {_e}")
        sys.exit(1)

    try:
        install_bucket = s3.Bucket(bucket)
        for path, subdirs, files in os.walk(f"{dist_directory}"):
            path = path.replace("\\", "/")
            for file in files:
                full_path = os.path.join(path, file)
                find_upload_location = re.search(
                    f"(.+)/dist/{cluster_id}/(.+)", full_path
                )
                if find_upload_location:
                    upload_location = f"{cluster_id}/{find_upload_location.group(2)}"
                else:
                    print(
                        f"Unable to determine upload location. {full_path} does not match regex '(.+)/dist/{cluster_id}/(.+)'"
                    )
                    sys.exit(1)

                logger.info(
                    f"[+] Uploading {os.path.join(path, file)} to s3://{bucket}/{upload_location} "
                )
                install_bucket.upload_file(os.path.join(path, file), upload_location)

    except Exception as upload_error:
        logger.error(f"Error during upload {upload_error}")


def accepted_aws_resources(region: str) -> dict:
    # Retrieve all AWS resources. Currently only used to find all available SSH keypair
    logger.debug(f"Retrieving accepted AWS resources in region {region}")
    accepted_values = {}
    try:
        # TODO describe_key_pairs does not have pagination support as of 12 Dec 2023
        # So while this looks bad - it works
        accepted_values["accepted_keypairs"] = {}
        for key in ec2.describe_key_pairs().get("KeyPairs", []):
            accepted_values["accepted_keypairs"][key["KeyPairId"]] = key

        if not accepted_values.get("accepted_keypairs", {}):
            logger.error(
                f"No SSH Key Pairs found in region {region}. Please create one first and re-run the installer."
            )
            sys.exit(1)
    except ClientError as _err:
        logger.warning(
            f"Unable to list SSH keys, you will need to enter it manually or give ec2:Describe* IAM permission. {_err} "
        )
        accepted_values["accepted_keypairs"] = {}

    return accepted_values


def check_bucket_name_and_permission(bucket: str) -> bool:
    # Check for None
    if bucket is None:
        # print(f"[red]Invalid S3 bucket name: (NoneType encountered)[/red]")
        return False

    # Check bucket naming before sending to the API
    # Rules are based on: https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html
    # NOTE - if the bucket requires a dot (.) - modify the regex
    _s3_bucket_re = r"(?!(^(xn--|sthree-|sthree-configurator)|.+(-s3alias|--ol-s3)$))^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"

    if not re.match(_s3_bucket_re, bucket):
        logger.error(
            f"Invalid S3 bucket name: ({bucket}). Must match Regular Expression: {_s3_bucket_re}"
        )
        return False

    # Check if user has permission to the S3 bucket specified
    try:
        s3.meta.client.head_bucket(Bucket=bucket)
        return True
    except ClientError as _e:
        logger.error(
            f"The S3 bucket ({bucket}) does not exist or you have do not have permissions: {_e}"
        )
        return False
    except botocore.exceptions.ParamValidationError as _e:
        logger.error(f"The S3 bucket ({bucket}) is invalid: {_e}")
        return False
    except Exception as _e:
        logger.error(f"Error during bucket permission check: {_e}")
        return False


def _get_aws_pcs_by_region_vpc(
    region: str, vpc_id: str, vpc_subnets: list[str]
) -> dict:
    """
    Get a list of AWS PCS clusters in the region and attached to a VPCId
    """

    logger.debug(
        f"Retrieving AWS PCS clusters in region {region} / VPC {vpc_id} / Subnets: {vpc_subnets}"
    )
    pcs = boto3.client("pcs", region_name=region)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "Elapsed:",
        TimeElapsedColumn(),
    ) as progress:
        count = 1

        _aws_pcs_task = progress.add_task(
            description=f"Discovering AWS PCS clusters in AWS region {region} / VPC {vpc_id}",
            start=False,
        )
        progress.start_task(_aws_pcs_task)

        _aws_pcs_clusters = defaultdict(dict)
        _aws_pcs_choices = {}

        # TODO - Show a progress bar
        _pcs_paginator = pcs.get_paginator("list_clusters")
        _pcs_iterator = _pcs_paginator.paginate()

        for _pcs_page in _pcs_iterator:
            for _cluster in _pcs_page.get("clusters", []):
                # Make sure the cluster belongs to our VPC

                # Make sure the cluster is in ACTIVE state
                if _cluster.get("status", "") != "ACTIVE":
                    progress.console.log(
                        f"Skipping cluster {_cluster['clusterName']} as it is not in ACTIVE state: {_cluster.get('status', '')}"
                    )
                    continue

                # Make sure the cluster has configurations set
                if not (_cluster_id := _cluster.get("id", "")):
                    progress.console.log(
                        f"Skipping cluster {_cluster['clusterName']} as it has no ID"
                    )
                    continue

                progress.console.log(
                    f"Probing AWS PCS cluster [bold green]{_cluster_id}[/] for configuration..."
                )

                _pcs_cluster_config = pcs.get_cluster(clusterIdentifier=_cluster_id)
                logger.debug(
                    f"Cluster {_cluster_id} has configuration: {_pcs_cluster_config}"
                )

                if not _pcs_cluster_config:
                    progress.console.log(
                        f"Skipping cluster {_cluster_id} as it has no configuration"
                    )
                    continue

                if not (_pcs_cluster := _pcs_cluster_config.get("cluster", {})):
                    progress.console.log(
                        f"Skipping cluster {_cluster_id} as it is not a cluster"
                    )
                    continue

                if not _pcs_cluster.get("status", "") == "ACTIVE":
                    progress.console.log(
                        f"Skipping cluster {_cluster_id} as it is not ACTIVE (2nd check)"
                    )
                    continue

                if not (_pcs_cluster_id := _pcs_cluster.get("id", "")):
                    progress.console.log(
                        f"Skipping cluster {_cluster_id} as it is invalid"
                    )
                    continue

                if _pcs_cluster_id not in _aws_pcs_clusters:
                    logger.debug(f"creating entry for AWS PCS {_pcs_cluster_id}")
                    _aws_pcs_clusters[count] = {}
                    _aws_pcs_clusters[count]["id"] = _pcs_cluster_id
                    _aws_pcs_clusters[count]["status"] = _pcs_cluster.get("status", "")

                if not (_scheduler_config := _pcs_cluster.get("scheduler", "")):
                    progress.console.log(
                        f"Skipping cluster {_cluster_id} as it is has invalid scheduler configuration"
                    )
                    continue

                logger.debug(f"AWS PCS Scheduler config is: {_scheduler_config}")

                if not (_scheduler_type := _scheduler_config.get("type", "").upper()):
                    progress.console.log(
                        f"Skipping cluster {_cluster_id} as it doesnt define a scheduler"
                    )
                    continue

                # TODO - Add more PCS schedulers here when they are supported
                if _scheduler_type not in {"SLURM"}:
                    progress.console.log(
                        f"Skipping cluster {_cluster_id} as it is not a supported scheduler"
                    )
                    continue

                _aws_pcs_clusters[count]["type"] = _scheduler_type
                logger.debug(f"AWS PCS Scheduler type is: {_scheduler_type}")

                match _scheduler_type:
                    case "SLURM":
                        # TODO FIXME - Match with our config file version
                        if _scheduler_config.get("version", "") != "23.11":
                            progress.console.log(
                                f"Skipping cluster {_cluster_id} as it is not a supported version of SLURM"
                            )
                            continue

                        _aws_pcs_clusters[count]["config"] = {}
                        for _conf_tree in {
                            "slurmConfiguration",
                            "networking",
                            "endpoints",
                        }:
                            if not (
                                _conf_tree_items := _pcs_cluster.get(_conf_tree, {})
                            ):
                                progress.console.log(
                                    f"Skipping cluster {_cluster_id} as it is missing {_conf_tree}"
                                )
                                continue
                            _aws_pcs_clusters[count]["config"][
                                _conf_tree
                            ] = _conf_tree_items

                        # Validate we have a matching subnet
                        progress.console.log(
                            f"Comparing Subnet information to SOCA cluster information..."
                        )
                        _subnet_match: bool = False
                        for _subnet in _aws_pcs_clusters[count]["config"]["networking"][
                            "subnetIds"
                        ]:
                            if _subnet in vpc_subnets:
                                _subnet_match = True
                                break
                        if not _subnet_match:
                            progress.console.log(
                                f"Skipping cluster {_cluster_id} as it is not in the same subnets as SOCA cluster"
                            )
                            continue

                    case _:
                        # This should already be caught above - just a precaution
                        progress.console.log(
                            f"Skipping cluster {_cluster_id} as it is not a SLURM scheduler"
                        )
                        continue

                _cluster_subnet_information: str = "\n".join(
                    _aws_pcs_clusters[count]["config"]["networking"]["subnetIds"]
                )
                _endpoint_information: list = []
                for _endpoint in _aws_pcs_clusters[count]["config"]["endpoints"]:
                    _endpoint_information.append(
                        f"{_endpoint['type']}: {_endpoint['privateIpAddress']}:{_endpoint['port']}"
                    )
                _endpoint_information_str: str = "\n".join(_endpoint_information)

                # Form our return to caller
                _aws_pcs_choices[count] = {
                    "type": _aws_pcs_clusters[count]["type"],
                    "id": _aws_pcs_clusters[count]["id"],
                    "status": _aws_pcs_clusters[count]["status"],
                    "endpoints": _aws_pcs_clusters[count]["config"].get(
                        "endpoints", {}
                    ),
                    "slurmConfiguration": _aws_pcs_clusters[count]["config"].get(
                        "slurmConfiguration", {}
                    ),
                    "networking": _aws_pcs_clusters[count]["config"].get(
                        "networking", {}
                    ),
                    "subnet_information": f"{_cluster_subnet_information}",
                    "endpoint_information": f"{_endpoint_information_str}",
                }

                logger.debug(f"All choices is now: {_aws_pcs_choices}")
                count += 1

    # We should have a populated _aws_pcs_clusters[_pcs_cluster_id] with all of our config

    logger.debug(f"Returning all AWS PCS choices: {_aws_pcs_choices}")
    return {"success": True, "message": _aws_pcs_choices}


def _get_filesystems_by_vpc(region: str, vpc_id: str) -> dict:
    # console = Console(record=True)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "Elapsed:",
        TimeElapsedColumn(),
    ) as progress:
        filesystems = {}
        count = 1

        efs_task = progress.add_task(
            description="Discovering EFS filesystems", start=False
        )
        fsx_task = progress.add_task(
            description="Discovering FSx filesystems", start=False
        )

        progress.start_task(efs_task)
        progress.console.log(
            f"[bold green]Retrieving EFS Filesystems from {region}/{vpc_id} ...[/bold green]"
        )

        efs_paginator = efs.get_paginator("describe_file_systems")
        efs_iterator = efs_paginator.paginate()

        for page in efs_iterator:
            for filesystem in page.get("FileSystems", []):
                _fs_id: str = filesystem.get("FileSystemId", "unknown")

                # Shouldn't happen
                if _fs_id == "unknown":
                    progress.console.log(f"[yellow] Skipping filesystem {_fs_id}")
                    continue

                # check for lifecycle
                if filesystem.get("LifeCycleState", "unknown").upper() not in {
                    "AVAILABLE"
                }:
                    progress.console.log(
                        f"[yellow]Skipping EFS {_fs_id} - filesystem Lifecycle is not ready (must be AVAILABLE)[/yellow]"
                    )
                    continue

                verified_vpc = False

                _mount_target_count: int = filesystem.get("NumberOfMountTargets", 0)
                if _mount_target_count <= 0:
                    progress.console.log(
                        f"[yellow]Skipping EFS filesystem {_fs_id} - no mount targets available[/yellow]"
                    )
                    continue

                progress.console.log(
                    f"[cyan]Processing mount targets for EFS {_fs_id}[/cyan]"
                )
                mount_targets = efs.describe_mount_targets(FileSystemId=_fs_id)[
                    "MountTargets"
                ]

                for mount_target in mount_targets:
                    time.sleep(
                        0.100
                    )  # Prevent Throttle Exceptions that can take place in dense EFS environments
                    if mount_target["VpcId"] == vpc_id:
                        verified_vpc = True

                if verified_vpc is True:
                    _fs_name: str = (
                        filesystem["Name"] if "Name" in filesystem.keys() else "EFS: "
                    )
                    _fs_size: int = filesystem.get("SizeInBytes", {}).get("Value", 0)
                    _fs_size_str: str = format_byte_size(_fs_size)
                    progress.console.log(
                        f"[cyan]Discovered EFS filesystem {_fs_id} ({_fs_name}) ({_fs_size_str})[/cyan]"
                    )

                    # Populate features for EFS
                    _fs_perf_mode: str = filesystem.get("PerformanceMode", "unknown")
                    _fs_features_str: str = ""

                    if _fs_perf_mode == "generalPurpose":
                        _fs_throughputmode = filesystem.get("ThroughputMode", "unknown")

                        if _fs_throughputmode == "provisioned":
                            _fs_throughput_rate: float = filesystem.get(
                                "ProvisionedThroughputInMibps", 0.0
                            )
                            _fs_features_str = (
                                f"GP/Provisioned/{_fs_throughput_rate} MiB/s"
                            )
                        else:
                            _fs_features_str = (
                                f"GP/"
                                + filesystem.get(
                                    "ThroughputMode", "unknown"
                                ).capitalize()
                            )

                    else:
                        progress.console.log(
                            f"[red]Skipping Filesystem - Unknown performance mode for filesystem {_fs_id} ({_fs_name})[/red]"
                        )
                        continue

                    # TODO alternate partition DNS suffix determination
                    _fs_id_fqdn: str = f"{_fs_id}.efs.{region}.amazonaws.com"

                    if filesystem["FileSystemId"]:
                        filesystems[count] = {
                            "id": _fs_id,
                            "name": _fs_id_fqdn,
                            "size": _fs_size_str,
                            "fs_type": "efs",
                            "description": f"{_fs_name} {_fs_id_fqdn}",
                            "features": _fs_features_str,
                        }
                        count += 1

        # efs_count = count - 1

        progress.console.log(
            f"[bold green]Retrieving FSx Filesystems from {region}/{vpc_id} ...[/bold green]"
        )
        progress.start_task(fsx_task)
        fsx_paginator = fsx.get_paginator("describe_file_systems")
        fsx_iterator = fsx_paginator.paginate()

        for page in fsx_iterator:
            for filesystem in page.get("FileSystems", []):
                _fs_id: str = filesystem.get("FileSystemId", "")

                if not _fs_id:
                    continue

                # Check for proper Lifecycle

                _fs_lifecycle: str = filesystem.get("Lifecycle", "unknown-lifecycle")
                if _fs_lifecycle.upper() not in {
                    "AVAILABLE",
                    "UPDATING",
                }:
                    progress.console.log(
                        f"[yellow]Skipping FSx filesystem {filesystem['FileSystemId']} ({fsx_type}) - filesystem Lifecycle is not ready (status {_fs_lifecycle})[/yellow]"
                    )
                    continue

                _fs_features_list: list = []

                fsx_type = filesystem.get("FileSystemType", "unknown-type")

                # TODO - Add more FSx support here
                # if fsx_type.upper() not in {'WINDOWS', 'LUSTRE', 'ONTAP', 'OPENZFS'}:
                if fsx_type.upper() not in {"LUSTRE"}:
                    progress.console.log(
                        f"[yellow]Skipping unsupported FSx type ({fsx_type}) for {filesystem['FileSystemId']}[/yellow]"
                    )
                    continue

                # Skip filesystems we have selected and ones that do not match our VPC
                if filesystem.get("VpcId", "") != vpc_id:
                    progress.console.log(
                        f"[yellow]Skipping FSx {filesystem['FileSystemId']} - not in our VPC[/yellow]"
                    )
                    continue

                _fs_size_str: str = format_byte_size(
                    num=filesystem.get("StorageCapacity", 0) * 1024 * 1024 * 1024
                )

                resource_name: str = ""
                for tag in filesystem.get("Tags", []):
                    if tag.get("Key", "") == "Name":
                        resource_name = tag.get("Value", "unnamed")

                progress.console.log(
                    f"[cyan]Discovered FSx/{fsx_type.capitalize()} filesystem {filesystem['FileSystemId']} ({resource_name}) ({_fs_size_str})[/cyan]"
                )

                #
                _dns_name: str = filesystem.get("DNSName", "UnknownDNS")

                match fsx_type:

                    case "ONTAP":
                        _key: str = "OntapConfiguration"

                    case "LUSTRE":
                        _key: str = "LustreConfiguration"

                    case "WINDOWS":
                        _key: str = "WindowsConfiguration"

                    case "OPENZFS":
                        _key: str = "OpenZFSConfiguration"

                    case _:
                        continue

                # Read our specific deployment type from our FSx config portion
                _deployment_type: str = filesystem.get(_key, {}).get(
                    "DeploymentType", ""
                )

                if _deployment_type:
                    _fs_features_list.append(_deployment_type)

                # Other filesystems may use this later - for now only Lustre seems to populate this
                _fs_version: str = filesystem.get("FileSystemTypeVersion", "")
                if _fs_version:
                    _fs_features_list.append(f"Version: {_fs_version}")

                # For FSx/ONTAP - we need to enumerate the SVMs to properly make the filesystems mountable
                if fsx_type.upper() == "ONTAP":
                    logger.debug(
                        f"Enumerating volumes for FSx/ONTAP filesystem {filesystem.get('FileSystemId')}"
                    )
                    progress.console.log(
                        f"[cyan]Enumerating volumes for FSx/ONTAP filesystem {filesystem.get('FileSystemId')}[/cyan]"
                    )

                    _volume_paginator = fsx.get_paginator("describe_volumes")
                    _volume_iterator = _volume_paginator.paginate(
                        Filters=[
                            {
                                "Name": "file-system-id",
                                "Values": [filesystem.get("FileSystemId")],
                            }
                        ],
                    )

                    # Stage-1 - query the Volumes
                    _ontap_volumes: dict = {}
                    _ontap_svms_to_enum: list = []
                    _ontap_fsids: list = []

                    for _vol_page in _volume_iterator:
                        for _volume in _vol_page.get("Volumes", []):
                            # Name is optional?
                            _vol_name: str = _volume.get("Name", "")
                            _vol_fsid: str = _volume.get("FileSystemId", "")
                            _vol_ontap_config: dict = _volume.get(
                                "OntapConfiguration", {}
                            )

                            if not _vol_ontap_config:
                                logger.error(
                                    f"ONTAP configuration not found - probable defect"
                                )
                                sys.exit(1)

                            _vol_id: str = _volume.get("VolumeId", "")
                            if not _vol_id:
                                logger.error(f"Volume ID not found - probable defect")
                                sys.exit(1)

                            if not _vol_fsid or _vol_fsid != filesystem.get(
                                "FileSystemId"
                            ):
                                logger.error(
                                    f"Volume mismatch for FileSystemId - probable defect"
                                )
                                sys.exit(1)

                            _vol_type: str = _volume.get("VolumeType", "")
                            if not _vol_type or _vol_type != "ONTAP":
                                logger.error(f"Volume type not found - probable defect")
                                sys.exit(1)

                            # Ignore the SVM root volume
                            _vol_is_svm_root: bool = _vol_ontap_config.get(
                                "StorageVirtualMachineRoot", True
                            )
                            if _vol_is_svm_root:
                                logger.debug(
                                    f"Skipping volume {_vol_name} - SVM root volume"
                                )
                                progress.console.log(
                                    f"Skipping volume {_vol_name} - SVM root volume"
                                )
                                continue

                            _vol_ontap_type: str = _vol_ontap_config.get(
                                "OntapVolumeType", ""
                            )
                            if not _vol_ontap_type:
                                logger.error(
                                    f"ONTAP volume type not found - probable defect"
                                )
                                continue

                            if _vol_ontap_type.upper() not in {"RW", "LS"}:
                                logger.debug(
                                    f"Skipping volume {_vol_name} - not an RW or LS ONTAP volume type"
                                )
                                progress.console.log(
                                    f"Skipping volume {_vol_name} - not an RW or LS ONTAP volume type"
                                )
                                continue

                            _volume_id: str = _volume.get("VolumeId", "")
                            if not _volume_id:
                                logger.warning(f"Volume ID not found - skipping")
                                continue

                            _junction_path: str = _vol_ontap_config.get(
                                "JunctionPath", ""
                            )
                            if not _junction_path:
                                logger.warning(f"Junction path not found - skipping")
                                continue

                            _volume_size: int = _vol_ontap_config.get("SizeInBytes", 0)
                            if not _volume_size:
                                logger.warning(f"Volume size not found - skipping")
                                continue
                            _volume_size_str: str = format_byte_size(_volume_size)

                            _vol_svm_id: str = _vol_ontap_config.get(
                                "StorageVirtualMachineId", ""
                            )
                            if not _vol_svm_id:
                                logger.debug(f"SVM not found - skipping")
                                progress.console.log(
                                    f"Skipping {_vol_name} - No SVM record found"
                                )
                                continue

                            progress.console.log(
                                f"[cyan]Discovered volume {_volume_id} ({_junction_path}) ({_volume_size_str})[/cyan]"
                            )
                            logger.debug(
                                f"Discovered volume {_volume_id} ({_junction_path}) ({_volume_size_str})"
                            )

                            # Store this volume as a possible mount target
                            if _volume_id not in _ontap_volumes:
                                _ontap_volumes[_volume_id] = _volume
                                _ontap_fsids.append(_vol_fsid)

                            # Make sure to query the SVM that is responsible for this volume
                            if _vol_svm_id not in _ontap_svms_to_enum:
                                logger.debug(f"SVM {_vol_svm_id} added to be probed)")
                                progress.console.log(
                                    f"[cyan]SVM {_vol_svm_id} will be queried[/cyan]"
                                )
                                _ontap_svms_to_enum.append(_vol_svm_id)
                            else:
                                # Since an SVM can be responsible for multiple volumes - we may have already seen it
                                progress.console.log(
                                    f"[cyan]SVM {_vol_svm_id} already planned to be queried[/cyan]"
                                )
                                logger.debug(
                                    f"SVM {_vol_svm_id} is already planned to be probed"
                                )

                    logger.debug(
                        f"Stage 1 complete - Collected ONTAP volumes: {_ontap_volumes}"
                    )
                    # If we didn't collect any - we shouldn't need to enum the SVMs
                    # Stage 2 - Now that we have volume information - we need to resolve the SVM information for
                    # mounting the filesystems
                    _ontap_svm_count: int = 0
                    _ontap_svm_dict: dict = {}

                    if _ontap_volumes and _ontap_svms_to_enum:

                        logger.debug(
                            f"Enumerating SVMs for FSx/ONTAP filesystem {filesystem.get('FileSystemId')} / {_ontap_svms_to_enum}"
                        )
                        progress.console.log(
                            f"[cyan]Enumerating SVMs for FSx/ONTAP filesystem {filesystem.get('FileSystemId')}[/cyan]"
                        )

                        _ontap_svm_paginator = fsx.get_paginator(
                            "describe_storage_virtual_machines"
                        )
                        # TODO - validate the max size of the SVM listing that the API call can take and chunk as needed
                        _ontap_svm_iterator = _ontap_svm_paginator.paginate(
                            StorageVirtualMachineIds=_ontap_svms_to_enum
                        )

                        for _svm_page in _ontap_svm_iterator:
                            for _svm in _svm_page.get("StorageVirtualMachines", []):

                                _svm_name: str = _svm.get("Name", "")
                                _svm_id: str = _svm.get("StorageVirtualMachineId", "")
                                _svm_fsid: str = _svm.get("FileSystemId", "")

                                # SVM name is optional?

                                # We must know how to link this SVM to a filesystem ID
                                if not _svm_fsid:
                                    logger.debug(
                                        f"SVM {_svm_name} has no FSID {_svm_fsid} - skipping"
                                    )
                                    continue

                                # We _should_ only see the ones we got from the API call of the interesting SVMs
                                # , but we check here just in case.
                                if _svm_fsid not in _ontap_fsids:
                                    logger.debug(
                                        f"SVM {_svm_name} has no matching FSID - skipping"
                                    )
                                    continue

                                progress.console.log(
                                    f"[cyan]Discovered SVM {_svm_name} for FSx/ONTAP filesystem {_svm_fsid}[/cyan]"
                                )
                                logger.debug(
                                    f"Discovered SVM {_svm_name} for FSx/ONTAP filesystem {_svm_fsid}"
                                )

                                # Only allow full created SVMs
                                _svm_lifecycle: str = _svm.get("Lifecycle", "")
                                if _svm_lifecycle.upper() not in {"CREATED"}:
                                    progress.console.log(
                                        f"[yellow]Skipping SVM {_svm_name} - not in CREATED state ({_svm_lifecycle})[/yellow]"
                                    )
                                    continue

                                # endpoint enum
                                # TODO - handle list / multiple IP addresses for the SVM
                                _nfs_ip_address: str = (
                                    _svm.get("Endpoints", {})
                                    .get("Nfs", {})
                                    .get("IpAddresses", "")[0]
                                )

                                # We add each SVM as technically they are discrete mount targets
                                if _nfs_ip_address:
                                    progress.console.log(
                                        f"Discovered SVM {_svm_name} NFS via {_nfs_ip_address}"
                                    )
                                    logger.debug(
                                        f"Discovered SVM {_svm_name} NFS via {_nfs_ip_address}"
                                    )
                                    # Add each SVM as a unique filesystem choice
                                    _ontap_svm_dict[_svm_id] = _svm
                                    _ontap_svm_count += 1
                                    continue
                                else:
                                    progress.console.log(
                                        f"Unable to determine NFS endpoint for SVM {_svm_name}"
                                    )
                                    logger.warning(
                                        f"Unable to determine NFS endpoint for SVM {_svm_name}"
                                    )
                                    continue

                        logger.debug(
                            f"Total SVMs found: {_ontap_svm_count}: {_ontap_svm_dict}"
                        )

                        # Now that we are back from SVM polling - assemble the data into
                        # a selectable menu list compatible with other filesystems
                        if _ontap_volumes and _ontap_svm_dict:

                            logger.debug(
                                f"Final pass - All FSx/ONTAP volumes: {_ontap_volumes}"
                            )

                            # Do an initial copy of our previous / FSx-wide features (deployment type, etc.)

                            for _ontap_vol_i in _ontap_volumes:
                                # Make sure to copy the items - not point to the parent list!
                                _ontap_fs_features_list: list = [*_fs_features_list]
                                logger.debug(
                                    f"Final pass of ONTAP volumes: {_ontap_vol_i} / FS Features now: {_ontap_fs_features_list}"
                                )

                                _vol: dict = _ontap_volumes.get(_ontap_vol_i, {})
                                _vol_ontap_config: dict = _vol.get(
                                    "OntapConfiguration", {}
                                )

                                _vol_name: str = _vol.get("Name", "")

                                _vol_fsid: str = _vol.get("FileSystemId", "")
                                _vol_svmid: str = _vol_ontap_config.get(
                                    "StorageVirtualMachineId", ""
                                )

                                logger.debug(
                                    f"Query SVM details for: {_vol_svmid}: {_ontap_svm_dict.get(_vol_svmid, {})}"
                                )

                                _svm_name: str = _ontap_svm_dict.get(
                                    _vol_svmid, {}
                                ).get("Name", "")

                                _nfs_ip_address_list: list = (
                                    _ontap_svm_dict.get(_vol_svmid, {})
                                    .get("Endpoints", {})
                                    .get("Nfs", {})
                                    .get("IpAddresses", [])
                                )
                                if _nfs_ip_address_list:
                                    logger.debug(
                                        f"SVM: {_svm_name} - NFS IP: {_nfs_ip_address_list} - taking first entry"
                                    )
                                    _nfs_ip_address: str = (
                                        _nfs_ip_address_list[0]
                                        if _nfs_ip_address_list
                                        else ""
                                    )
                                    _ontap_fs_features_list.append(
                                        f"NFS: {_nfs_ip_address}"
                                    )
                                else:
                                    logger.error(f"Empty NFS IP list for SVM. Exiting")
                                    sys.exit(1)

                                _fs_size_str: str = format_byte_size(
                                    int(
                                        _vol.get("OntapConfiguration", {}).get(
                                            "SizeInBytes", 0
                                        )
                                    )
                                )

                                _fs_junction_path: str = _vol.get(
                                    "OntapConfiguration", {}
                                ).get("JunctionPath", "")

                                _fs_security_style: str = _vol.get(
                                    "OntapConfiguration", {}
                                ).get("SecurityStyle", "")
                                _ontap_fs_features_list.append(
                                    f"Security: {_fs_security_style}"
                                )

                                # Finally - construct a menu entry/item of a selectable filesystem
                                filesystems[count] = {
                                    "id": _ontap_vol_i,
                                    "name": f"{_vol_name} via SVM {_svm_name} ({_vol_svmid})\nPath: {_fs_junction_path}",
                                    "dns_name": _nfs_ip_address,
                                    "size": _fs_size_str,
                                    "fs_type": "fsx_ontap",
                                    "description": f"FSx/ONTAP: {_vol_name} via SVM {_svm_name} - {_nfs_ip_address}:{_fs_junction_path}",
                                    "features": "\n".join(_ontap_fs_features_list),
                                }
                                count += 1
                                continue

                else:
                    # non-FSx/ONTAP
                    filesystems[count] = {
                        "id": f"{filesystem['FileSystemId']}",
                        "name": resource_name,
                        "dns_name": _dns_name,
                        "size": _fs_size_str,
                        "fs_type": f"fsx_{fsx_type.lower()}",
                        "description": f"FSx/{fsx_type.upper()}: {resource_name if resource_name else f'FSx/{fsx_type.upper()}: '} {_dns_name}",
                        "features": "\n".join(_fs_features_list),
                    }
                    count += 1

    return filesystems


def get_install_parameters():
    # Retrieve User Specified Variables
    print(
        "\n====== Validating [red]S[blue]O[magenta]C[yellow]A[default] Parameters ======\n"
    )

    install_parameters["cluster_name"] = get_input(
        prompt=f"{install_phases.get('cluster_name', 'unk-prompt')}",
        specified_value=args.name,
        expected_answers="",
        show_expected_answers=False,
        show_default_answer=False,
        expected_type=str,
    )

    while (
        len(install_parameters["cluster_name"]) < 3
        or len(install_parameters["cluster_name"]) > 11
    ):
        print(
            f"[red]SOCA cluster name must greater than 3 chars and shorter than 11 characters (soca- is automatically added as a prefix) "
        )
        install_parameters["cluster_name"] = get_input(
            prompt=f"{install_phases.get('cluster_name', 'unk-prompt')}",
            specified_value=None,
            expected_answers=None,
            expected_type=str,
        )

    # Sanitize cluster name (remove any non-alphanumerical character) or generate random cluster identifier
    sanitized_cluster_id = re.sub(r"\W+", "-", install_parameters["cluster_name"])
    sanitized_cluster_id = re.sub(
        r"soca-", "", sanitized_cluster_id
    )  # remove "soca-" if specified by the user
    install_parameters["cluster_id"] = (
        f"soca-{sanitized_cluster_id.lower()}"  # do not remove "soca-" prefix or DCV IAM permission will not be working.
    )

    install_parameters["bucket"] = get_input(
        prompt=f"{install_phases.get('bucket', 'unk-prompt')}",
        specified_value=args.bucket,
        expected_answers=None,
        expected_type=str,
        show_expected_answers=False,
        show_default_answer=False,
    )

    while check_bucket_name_and_permission(install_parameters["bucket"]) is False:
        install_parameters["bucket"] = get_input(
            prompt=f"{install_phases.get('bucket', 'unk-prompt')}",
            specified_value=None,
            expected_answers=None,
            expected_type=str,
            show_expected_answers=False,
            show_default_answer=False,
        )

    _base_os_available = {
        "amazonlinux2023": {"visible": True},
        # amazonlinux2 is purposely kept 2nd in the list and defaulted to make sure the prompting
        # works as expected
        "amazonlinux2": {
            "default": True,
            "visible": True,
        },
        "centos7": {"visible": False},
        "centos8": {"visible": False},
        "rhel7": {"visible": False},
        "rhel8": {"visible": True},
        "rhel9": {"visible": True},
        "rocky8": {"visible": True},
        "rocky9": {"visible": True},
        "ubuntu2204": {"visible": True},
        "ubuntu2404": {"visible": True},
    }

    # Generate our BaseOS prompt listing
    _baseos_visible_list: list = []
    for _potential_baseos, _options in _base_os_available.items():
        _is_baseos_visible: bool = _options.get("visible", False)
        logger.debug(
            f"Potential BaseOS: {_potential_baseos} - Visible: {_is_baseos_visible}"
        )
        if _is_baseos_visible:
            _baseos_visible_list.append(_potential_baseos)

    # Make the prompt look like the auto-generated prompts
    _base_os_prompt_str: str = "/".join(_baseos_visible_list)

    logger.debug(
        f"Final BaseOS prompt info: {_base_os_prompt_str}   List: {_baseos_visible_list}"
    )

    install_parameters["base_os"] = get_input(
        prompt=f"{install_phases.get('baseos', 'unk-prompt')} [magenta][b]\\[{_base_os_prompt_str}][/b][/magenta]",
        specified_value=args.base_os,
        expected_answers=_base_os_available,
        expected_type=str,
        # show_expected_answers needs to be False to prevent generating the entire list of
        # visible and non-visible choices - hence we manually construct our prompt and set
        # show_expected_answers to False
        show_expected_answers=False,
    )

    keypair_table = Table(title=f"SSH Key Pairs", show_lines=True, highlight=True)

    keypair_table.add_column(header="#", justify="center", width=4, no_wrap=True)
    keypair_table.add_column(
        header="Keypair ID", justify="center", width=21, no_wrap=True
    )
    keypair_table.add_column(header="Name", justify="center", width=32, no_wrap=True)
    keypair_table.add_column(header="Creation", justify="center")
    keypair_table.add_column(header="Fingerprint", justify="center", no_wrap=True)

    _kp_n = 1
    logger.debug(f"keypairs: {accepted_aws_values.get('accepted_keypairs', {})}")
    _kp_map = {}

    for keypair in accepted_aws_values.get("accepted_keypairs", {}):
        logger.debug(f"Processing keypair: {keypair=}")
        _key_id_str: str = accepted_aws_values["accepted_keypairs"][keypair].get(
            "KeyPairId"
        )
        _key_name_str: str = accepted_aws_values["accepted_keypairs"][keypair].get(
            "KeyName"
        )
        _key_type_str: str = accepted_aws_values["accepted_keypairs"][keypair].get(
            "KeyType"
        )
        _key_creation_date_str: str = str(
            accepted_aws_values["accepted_keypairs"][keypair].get("CreateTime")
        )
        _key_fingerprint_str: str = accepted_aws_values["accepted_keypairs"][
            keypair
        ].get("KeyFingerprint")
        if _kp_n not in _kp_map:
            _kp_map[_kp_n] = {}

        _kp_map[_kp_n] = {
            "KeyIndex": _kp_n,  # This is just our running tally
            "KeyPairId": _key_id_str,
            "KeyName": _key_name_str,
            "KeyFingerprint": _key_fingerprint_str,
            "KeyType": _key_type_str,
        }

        keypair_table.add_row(
            str(_kp_n),
            _key_id_str,
            f"{_key_name_str} ({_key_type_str})",
            _key_creation_date_str,
            _key_fingerprint_str,
        )
        _kp_n += 1

    _selected_key_name: str = ""
    _spec_value_str: str = ""

    if args.ssh_keypair:
        _spec_value_str = next(
            (
                v.get("KeyName")
                for k, v in _kp_map.items()
                if v.get("KeyName") == args.ssh_keypair
            ),
            "",
        )

        if not _spec_value_str:
            logger.warning(
                f"Keypair {args.ssh_keypair} not found. Please confirm region and select a keypair"
            )
        else:
            _selected_key_name = _spec_value_str

    if not _selected_key_name:
        # make sure we don't draw emojis in SSH fingerprints
        _console = Console(emoji=False)
        _console.print(keypair_table)

        _keypair_selection = get_input(
            prompt=f"{install_phases.get('key_pair', 'unk-prompt')}",
            specified_value=None,
            expected_answers=[str(_kp) for _kp in range(1, _kp_n)],
            expected_type=int,
            show_expected_answers=False,
            show_default_answer=True,
        )
        logger.debug(f"Keypair selection: {_keypair_selection}")
        _spec_value_str = next(
            (
                v.get("KeyName")
                for k, v in _kp_map.items()
                if v.get("KeyIndex") == _keypair_selection
            ),
            "",
        )
        logger.debug(
            f"Keypair selection _spec_value_str:  {_spec_value_str} - KeyMap: {_kp_map}"
        )

    _selected_key_name = _spec_value_str

    logger.debug(f"Final keypair name: {_selected_key_name} from {_spec_value_str}")
    install_parameters["ssh_keypair"] = _selected_key_name

    # Validate the prefix list id
    if args.prefix_list_id:
        try:
            found_prefix_list_id = ec2.describe_managed_prefix_lists(
                PrefixListIds=[args.prefix_list_id]
            )["PrefixLists"][0]["PrefixListId"]
            if found_prefix_list_id != args.prefix_list_id:
                raise RuntimeError(
                    f"Found prefix list {found_prefix_list_id} does not match {args.prefix_list_id}. This is a programming error; please create an issue."
                )
            else:
                install_parameters["prefix_list_id"] = args.prefix_list_id
        except Exception as _e:
            logger.error(
                f"{args.prefix_list_id} not found. Check that it exists and starts with pl-.\nException:\n{_e} "
            )
            sys.exit(1)

    install_parameters["custom_ami"] = args.custom_ami if args.custom_ami else None

    # Network Configuration
    cidr_regex = r"^(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])(\/([0-9]|[1-2][0-9]|3[0-2]))$"

    if not args.vpc_cidr:
        choice_vpc = get_input(
            prompt=f"{install_phases.get('vpc', 'unk-prompt')}",
            specified_value=None,
            expected_answers=["new", "existing"],
            expected_type=str,
        )

        if choice_vpc == "new":

            while True:

                install_parameters["vpc_cidr"] = get_input(
                    prompt="What CIDR do you want to use for your VPC? We recommend 10.0.0.0/16",
                    specified_value=None,
                    expected_answers=None,
                    expected_type=str,
                    show_expected_answers=False,
                    show_default_answer=False,
                )

                if install_parameters["vpc_cidr"] is None:
                    logger.error(
                        f"Invalid CIDR {install_parameters['vpc_cidr']}. Format must be x.x.x.x/x (eg: 10.0.0.0/16)"
                    )
                elif not re.match(cidr_regex, install_parameters["vpc_cidr"]):
                    logger.error(
                        f"Invalid CIDR {install_parameters['vpc_cidr']}. Format must be x.x.x.x/x (eg: 10.0.0.0/16)"
                    )
                else:
                    logger.debug(f"Valid CIDR {install_parameters['vpc_cidr']}")
                    break

        else:
            # List all VPCs running on AWS account
            existing_vpc = FindExistingResource(
                region=install_parameters["region"],
                client_ip=install_parameters["client_ip"],
            ).find_vpc()

            if existing_vpc.get("success", False):
                install_parameters["vpc_id"] = existing_vpc["message"]["id"]
                install_parameters["vpc_cidr"] = existing_vpc["message"]["cidr"]
            else:
                logger.error(
                    f"Unable to find VPC in the configured AWS Account - exiting..."
                )
                sys.exit(1)

            # List all Subnets
            if install_props.Config.entry_points_subnets.lower() == "public":

                public_subnets = FindExistingResource(
                    region=install_parameters["region"],
                    client_ip=install_parameters["client_ip"],
                ).get_subnets(
                    vpc_id=install_parameters["vpc_id"],
                    environment="public",
                    selected_subnets=[],
                )

                if public_subnets.get("success", False):
                    install_parameters["public_subnets"] = base64.b64encode(
                        str(public_subnets["message"]).encode("utf-8")
                    ).decode("utf-8")
                else:
                    logger.error(f"Error: {public_subnets['message']}[default]")
                    sys.exit(1)

            else:
                public_subnets = {"success": False, "message": []}
                install_parameters["public_subnets"] = base64.b64encode(
                    str(public_subnets["message"]).encode("utf-8")
                ).decode("utf-8")

            private_subnets = FindExistingResource(
                region=install_parameters["region"],
                client_ip=install_parameters["client_ip"],
            ).get_subnets(
                vpc_id=install_parameters["vpc_id"],
                environment="private",
                selected_subnets=[],
            )

            if private_subnets.get("success", False):
                install_parameters["private_subnets"] = base64.b64encode(
                    str(private_subnets["message"]).encode("utf-8")
                ).decode("utf-8")
            else:
                logger.error(f"Error: {private_subnets['message']}[default]")
                sys.exit(1)

            vpc_azs = []
            for subnet in public_subnets["message"] + private_subnets["message"]:
                az = subnet.split(",")[1]
                if az not in vpc_azs:
                    vpc_azs.append(az)

                install_parameters["vpc_azs"] = ",".join(vpc_azs)

    else:
        # Existing VPC
        install_parameters["vpc_cidr"] = args.vpc_cidr
        while not re.match(cidr_regex, install_parameters["vpc_cidr"]):
            # TODO - Dupe with above - convert to a def
            while True:

                install_parameters["vpc_cidr"] = get_input(
                    prompt="What CIDR do you want to use for your VPC? We recommend 10.0.0.0/16",
                    specified_value=None,
                    expected_answers=None,
                    expected_type=str,
                    show_expected_answers=False,
                    show_default_answer=False,
                )

                if install_parameters["vpc_cidr"] is None:
                    logger.error(
                        f"Invalid CIDR {install_parameters['vpc_cidr']}. Format must be x.x.x.x/x (eg: 10.0.0.0/16)"
                    )
                elif not re.match(cidr_regex, install_parameters["vpc_cidr"]):
                    logger.error(
                        f"Invalid CIDR {install_parameters['vpc_cidr']}. Format must be x.x.x.x/x (eg: 10.0.0.0/16)"
                    )
                else:
                    logger.debug(f"Valid CIDR {install_parameters['vpc_cidr']}")
                    break

    # Security Groups Configuration (only possible if user installs to an existing VPC)
    if install_parameters["vpc_id"]:
        choice_security_groups = get_input(
            prompt=f"{install_phases.get('security_groups', 'unk-prompt')}",
            specified_value=None,
            expected_answers=["new", "existing"],
            expected_type=str,
        )

        if choice_security_groups == "existing":
            controller_sg = FindExistingResource(
                region=install_parameters["region"],
                client_ip=install_parameters["client_ip"],
            ).get_security_groups(install_parameters["vpc_id"], "controller", [])
            if controller_sg.get("success", False):
                install_parameters["controller_sg"] = controller_sg["message"]
            else:
                logger.error(f"{controller_sg.get('message', '')} ")
                sys.exit(1)

            compute_node_sg = FindExistingResource(
                install_parameters["region"], install_parameters["client_ip"]
            ).get_security_groups(
                install_parameters["vpc_id"],
                "compute nodes",
                install_parameters["controller_sg"],
            )
            if compute_node_sg["success"] is True:
                install_parameters["compute_node_sg"] = compute_node_sg["message"]
            else:
                logger.error(f"{compute_node_sg['message']} ")
                sys.exit(1)

            if install_props.Config.network.vpc_interface_endpoints:
                _vpc_endpoint_sg = FindExistingResource(
                    install_parameters["region"], install_parameters["client_ip"]
                ).get_security_groups(
                    install_parameters["vpc_id"],
                    "vpc endpoints",
                    install_parameters["controller_sg"],
                )
                if _vpc_endpoint_sg["success"] is True:
                    install_parameters["vpc_endpoint_sg"] = _vpc_endpoint_sg["message"]
                else:
                    logger.error(f"{_vpc_endpoint_sg['message']} ")
                    sys.exit(1)
            else:
                _vpc_endpoint_sg = None

    # AWS PCS (only possible if a user installs to an existing VPC / cluster)
    if install_parameters.get("vpc_id", ""):
        _aws_pcs_enabled: bool = False
        try:
            _aws_pcs_enabled: bool = install_props.Config.services.aws_pcs.enabled
        except Exception as _e:
            # Make sure this stays as .debug
            # otherwise it can appear to the screen for the user
            logger.debug(f"Unable to parse config for AWS PCS enablement: {_e}")
            _aws_pcs_enabled = False

        if not _aws_pcs_enabled:
            logger.debug(f"AWS PCS integration is disabled")
        else:
            logger.debug(f"AWS PCS is enabled")

            _pcs_subnet_list: list = []
            for _sn in public_subnets["message"] + private_subnets["message"]:
                _pcs_subnet_list.append(_sn.split(",")[0])

            _aws_pcs_clusters = _get_aws_pcs_by_region_vpc(
                region=args.region,
                vpc_id=install_parameters.get("vpc_id", ""),
                vpc_subnets=_pcs_subnet_list,
            )

            if _aws_pcs_clusters.get("success", False):
                logger.debug(f"Got back AWS PCS clusters: {_aws_pcs_clusters}")

                _aws_pcs_cluster_dict = _aws_pcs_clusters.get("message", {})
                logger.debug(f"AWS PCS Cluster dict is: {_aws_pcs_cluster_dict}")
                _aws_pcs_table = Table(
                    title=f"Select the AWS PCS cluster to link to in region {args.region} / VPC {install_parameters.get('vpc_id', '')}",
                    show_lines=True,
                    highlight=True,
                )

                for _col_name in [
                    "#",
                    "Type",
                    "ID",
                    "Status",
                    "Subnet Information",
                    "Endpoint Information",
                ]:
                    _aws_pcs_table.add_column(_col_name, justify="center")

                _cluster_id_int: int = 1
                # TODO - sort clusters?
                for _cluster in _aws_pcs_cluster_dict:
                    _aws_pcs_table.add_row(
                        str(_cluster_id_int),
                        _aws_pcs_cluster_dict.get(_cluster_id_int).get("type", ""),
                        _aws_pcs_cluster_dict.get(_cluster_id_int).get("id", ""),
                        _aws_pcs_cluster_dict.get(_cluster_id_int).get("status", ""),
                        _aws_pcs_cluster_dict.get(_cluster_id_int).get(
                            "subnet_information", ""
                        ),
                        _aws_pcs_cluster_dict.get(_cluster_id_int).get(
                            "endpoint_information", ""
                        ),
                    )
                    _cluster_id_int += 1

                print(_aws_pcs_table)

                choice_aws_pcs = get_input(
                    prompt="Select the AWS PCS cluster to link to",
                    specified_value=None,
                    expected_answers=list(range(1, _cluster_id_int)),
                    expected_type=int,
                    show_default_answer=True,
                    show_expected_answers=True,
                )

                logger.debug(f"You selected AWS PCS cluster {choice_aws_pcs}")

            logger.debug(f"Expect to exit now...")
            sys.exit(1)

    # Filesystem Configuration (only possible if user installs to an existing VPC)
    if install_parameters["vpc_id"]:
        choice_filesystem = get_input(
            prompt=f"{install_phases.get('filesystems', 'unk-prompt')}",
            specified_value=None,
            expected_answers=["new", "existing"],
            expected_type=str,
        )
        if choice_filesystem == "existing":
            # TODO - This needs to be reworked to poll the account _once_ versus for each get_fs() call.
            # As this makes it very slow for big / populated VPCs/accounts
            # List FS

            _selected_fs = []
            _filesystems_in_vpc = _get_filesystems_by_vpc(
                region=install_parameters["region"],
                vpc_id=install_parameters["vpc_id"],
            )

            fs_apps = FindExistingResource(
                region=install_parameters["region"],
                client_ip=install_parameters["client_ip"],
            ).get_fs(
                environment="/apps",
                vpc_id=install_parameters["vpc_id"],
                filesystems=_filesystems_in_vpc,
            )

            if fs_apps.get("success", False):
                install_parameters["fs_apps_provider"] = fs_apps["provider"]
                install_parameters["fs_apps"] = fs_apps["message"]
                _selected_fs.append(fs_apps["message"])
            else:
                logger.error(f"{fs_apps['message']} ")
                sys.exit(1)

            # Trim down the /data options after /apps is selected
            _filesystems_in_vpc = {
                k: v
                for k, v in _filesystems_in_vpc.items()
                if v["id"] not in _selected_fs
            }

            fs_data = FindExistingResource(
                region=install_parameters["region"],
                client_ip=install_parameters["client_ip"],
            ).get_fs(
                environment="/data",
                vpc_id=install_parameters["vpc_id"],
                filesystems=_filesystems_in_vpc,
                selected_fs=_selected_fs,
            )

            if fs_data.get("success", False):
                install_parameters["fs_data_provider"] = fs_data["provider"]
                install_parameters["fs_data"] = fs_data["message"]
                # TODO - this should no longer be possible?
                if install_parameters["fs_data"] == install_parameters["fs_apps"]:
                    logger.error(
                        f"Filesystem choice for /apps and /data must be different "
                    )
                    sys.exit(1)
            else:
                logger.error(f"{fs_data['message']} ")
                sys.exit(1)

            # Verify SG permissions
            if install_parameters["fs_apps"] or install_parameters["controller_sg"]:
                FindExistingResource(
                    install_parameters["region"], install_parameters["client_ip"]
                ).validate_sg_rules(
                    install_parameters,
                    check_fs=True if install_parameters["fs_apps"] else False,
                )
        else:
            # Using an existing VPC, but creating new filesystems
            # FIXME TODO - duplicated with the next chunk of code
            for _fs_obj in ("apps", "data"):
                install_parameters[f"fs_{_fs_obj}_provider"] = get_input(
                    prompt=f"{install_phases.get(f'{_fs_obj}_storage_provider', 'unk-prompt')}",
                    specified_value=(
                        args.fs_apps_provider
                        if _fs_obj == "apps"
                        else args.fs_data_provider
                    ),
                    expected_answers=["efs", "fsx_ontap", "fsx_lustre"],
                    expected_type=str,
                    show_default_answer=True,
                    show_expected_answers=True,
                )

    else:
        # Not using existing VPC or filesystem, prompt for new fs creation provider
        for _fs_obj in ("apps", "data"):
            install_parameters[f"fs_{_fs_obj}_provider"] = get_input(
                prompt=f"{install_phases.get(f'{_fs_obj}_storage_provider', 'unk-prompt')}",
                specified_value=(
                    args.fs_apps_provider
                    if _fs_obj == "apps"
                    else args.fs_data_provider
                ),
                expected_answers=["efs", "fsx_ontap", "fsx_lustre"],
                expected_type=str,
                show_default_answer=True,
                show_expected_answers=True,
            )

    # AWS Directory Service Managed Active Directory configuration (only possible when using existing VPC)
    if install_props.Config.directoryservice.provider == "activedirectory":
        if install_parameters["vpc_id"]:
            choice_mad = get_input(
                f"{install_phases.get('directory_service', 'unk-prompt')}",
                None,
                ["new", "existing"],
                str,
            )
            if choice_mad == "existing":
                directory_service = FindExistingResource(
                    install_parameters["region"], install_parameters["client_ip"]
                ).find_directory_services(install_parameters["vpc_id"])
                if directory_service["success"] is True:
                    install_parameters["directory_service_ds_user"] = get_input(
                        f"Username of a domain user with  admin permissions?",
                        None,
                        None,
                        str,
                    )
                    install_parameters["directory_service_ds_user_password"] = (
                        get_input(
                            f"Password of the domain user with admin permissions",
                            None,
                            None,
                            str,
                        )
                    )
                    install_parameters["directory_service"] = directory_service[
                        "message"
                    ]["id"]
                    install_parameters["directory_service_shortname"] = (
                        directory_service["message"]["netbios"]
                    )
                    install_parameters["directory_service_name"] = directory_service[
                        "message"
                    ]["name"]
                    install_parameters["directory_service_dns"] = directory_service[
                        "message"
                    ]["dns"]
                else:
                    logger.error(f"{directory_service['message']}")
                    sys.exit(1)

    # ElasticSearch Configuration (only possible when using existing VPC)
    if install_parameters["vpc_id"]:
        choice_es = get_input(
            prompt=f"{install_phases.get('analytics', 'unk-prompt')}",
            specified_value=None,
            expected_answers=["no", "yes"],
            expected_type=str,
        )

        if choice_es == "yes":
            elasticsearch_cluster = FindExistingResource(
                install_parameters["region"], install_parameters["client_ip"]
            ).find_elasticsearch(install_parameters["vpc_id"])

            if elasticsearch_cluster["success"] is True:
                install_parameters["os_domain"] = elasticsearch_cluster["message"][
                    "endpoint"
                ]
                install_parameters["os_endpoint"] = elasticsearch_cluster["message"][
                    "endpoint"
                ]
            else:
                logger.error(f"Error: {elasticsearch_cluster['message']} ")
                sys.exit(1)
    else:
        install_parameters["os_domain"] = None

    # IAM Roles configuration (only possible when using existing VPC)
    if install_parameters["vpc_id"]:
        choice_iam_roles = get_input(
            f"{install_phases.get('iam_roles', 'unk-prompt')}",
            None,
            ["new", "existing"],
            str,
        )
        if choice_iam_roles == "existing":
            controller_role = FindExistingResource(
                install_parameters["region"], install_parameters["client_ip"]
            ).get_iam_roles("controller")
            if controller_role["success"] is True:
                install_parameters["controller_role_name"] = controller_role["message"][
                    "name"
                ]
                install_parameters["controller_role_arn"] = controller_role["message"][
                    "arn"
                ]
            else:
                logger.error(f"{controller_role['message']} ")
                sys.exit(1)

            if (
                get_input(
                    f"Was this role generated by a previous SOCA deployment? If yes, are you also using the same S3 bucket?",
                    None,
                    ["yes", "no"],
                    str,
                )
                == "yes"
            ):
                install_parameters["controller_role_from_previous_soca_deployment"] = (
                    True
                )
            else:
                get_input(
                    f"[IMPORTANT] Make sure this role is assumed by 'ec2.amazon.com' and 'ssm.amazonaws.com'\n Type ok to continue ...",
                    None,
                    ["ok"],
                    str,
                    color="yellow",
                )

            compute_node_role = FindExistingResource(
                install_parameters["region"], install_parameters["client_ip"]
            ).get_iam_roles(
                "compute nodes",
                selected_roles=[install_parameters["controller_role_name"]],
            )
            if compute_node_role["success"] is True:
                install_parameters["compute_node_role_name"] = compute_node_role[
                    "message"
                ]["name"]
                install_parameters["compute_node_role_arn"] = compute_node_role[
                    "message"
                ]["arn"]
            else:
                print(f"[red]Error: {compute_node_role['message']} ")
                sys.exit(1)

            if (
                get_input(
                    f"Was this role generated by a previous SOCA deployment?",
                    None,
                    ["yes", "no"],
                    str,
                )
                == "yes"
            ):
                install_parameters[
                    "compute_node_role_from_previous_soca_deployment"
                ] = True
            else:
                get_input(
                    f"[IMPORTANT] Make sure this role is assumed by 'ec2.amazon.com' and 'ssm.amazonaws.com'\n Type ok to continue ...",
                    None,
                    ["ok"],
                    str,
                    color="yellow",
                )

            spotfleet_role = FindExistingResource(
                install_parameters["region"], install_parameters["client_ip"]
            ).get_iam_roles(
                "spot fleet",
                selected_roles=[
                    install_parameters["controller_role_name"],
                    install_parameters["compute_node_role_name"],
                ],
            )
            if spotfleet_role["success"] is True:
                install_parameters["spotfleet_role_name"] = spotfleet_role["message"][
                    "name"
                ]
                install_parameters["spotfleet_role_arn"] = spotfleet_role["message"][
                    "arn"
                ]
            else:
                print(f"[red]Error: {spotfleet_role['message']} ")
                sys.exit(1)

            if (
                get_input(
                    f"Was this role generated by a previous SOCA deployment?",
                    None,
                    ["yes", "no"],
                    str,
                )
                == "yes"
            ):
                install_parameters["spotfleet_role_from_previous_soca_deployment"] = (
                    True
                )
            else:
                get_input(
                    f"[IMPORTANT] Make sure this role is assumed by 'spotfleet.amazonaws.com'\n Type ok to continue ...",
                    None,
                    ["ok"],
                    str,
                    color="yellow",
                )
    logger.info(f"[green]Parameters are valid. ")


def validate_soca_config(user_specified_inputs, install_properties):
    # We run one final validation check at the end, comparing the installation properties from config.yml and user inputs
    print("\n====== Verifying SOCA Configuration  ======\n")
    errors = []
    exit_installer = False  # Installer will exit if user has invalid default_config.yml params that require a file edit

    if user_specified_inputs["vpc_id"]:
        private_subnets = ast.literal_eval(
            base64.b64decode(user_specified_inputs["private_subnets"]).decode("utf-8")
        )
        private_subnet_azs = [
            k.split(",")[1] for k in private_subnets if k.split(",")[1]
        ]
        public_subnets = ast.literal_eval(
            base64.b64decode(user_specified_inputs["public_subnets"]).decode("utf-8")
        )
        public_subnet_azs = [k.split(",")[1] for k in public_subnets]

    # if AZ is = 2, check if ES data nodes is 1,2 or a multiple. No restriction when using > 3 AZs
    if not user_specified_inputs["es_domain"]:
        if user_specified_inputs["vpc_id"]:
            max_azs = len(list(dict.fromkeys(private_subnet_azs)))
        else:
            max_azs = install_properties.Config.network.max_azs

        if max_azs == 2:
            # No limitation when using 3 or more AZs. 1 is not an option here
            data_nodes = install_properties.Config.analytics.data_nodes
            if (data_nodes % 2) == 0 or data_nodes <= 2:
                pass
            else:
                errors.append(
                    "Config > OpenSearch > data_nodes must be 1,2 or a multiple of 2."
                )
                exit_installer = True

    if user_specified_inputs["vpc_id"]:
        # Validate network configuration when using a custom VPC
        if len(list(dict.fromkeys(private_subnet_azs))) == 1:
            errors.append(
                f"Your private subnets are only configured to use a single AZ ({private_subnet_azs}). You must use at least 2 AZs for HA"
            )
        if len(list(dict.fromkeys(public_subnet_azs))) == 1:
            errors.append(
                f"Your public subnets are only configured to use a single AZ ({public_subnet_azs}). You must use at least 2 AZs for HA"
            )
    else:
        # check if az is min 2
        if install_properties.Config.network.max_azs < 2:
            errors.append("Config > Network > max_azs must be at least 2")
            exit_installer = True

    if not errors:
        logger.info(f"[green]Configuration is valid. ")
        return True
    else:
        for message in errors:
            logger.error(f"{message} ")

        logger.error(
            f"!! Unable to validate configuration. Please fix the errors listed above and try again. "
        )
        if exit_installer is True:
            sys.exit(1)
        else:
            return False


def override_keys(keys_to_override, install_properties):
    override_mapping: dict = {}
    _config_table = Table(
        title=f"Detected CLI Configuration Overrides", show_lines=True, highlight=True
    )
    _config_table.add_column(header="Key", justify="left", width=40, no_wrap=True)
    _config_table.add_column(header="Type", justify="left", width=10, no_wrap=True)
    _config_table.add_column(header="Value", justify="left", width=25, no_wrap=True)

    for key in keys_to_override:
        # print(f"Detected Key Override: {key}")
        override_info = key.split(",")
        if len(override_info) != 3:
            print(
                f"Override information must use the following format: '<key_name>,<type>,<new_value>' (ex: Config.termination_protection,Bool,False). Detected {key}"
            )
            sys.exit(1)
        else:
            key_name = override_info[0]
            value_type = override_info[1]
            key_value = override_info[2]

            _value_type: str = value_type.lower()

            match _value_type:
                case "bool":
                    if key_value.lower() == "true":
                        key_value = True
                    elif key_value.lower() == "false":
                        key_value = False
                    else:
                        logger.error(
                            f"{key_name} does not seem to be a valid boolean. Please specify either True or False."
                        )
                        sys.exit(1)
                case "str" | "string":
                    key_value = str(key_value)
                case "list":
                    key_value = key_value.split("+")
                case "int" | "integer":
                    try:
                        key_value = int(key_value)
                    except ValueError:
                        print(f"Expected {value_type} but detected {key_value}")
                        sys.exit(1)
                case _:
                    logger.error(
                        f"Value type must be bool/boolean/str/string/int/integer/list. Detected {_value_type}"
                    )
                    sys.exit(1)

        _config_table.add_row(
            str(key_name),
            str(value_type),
            str(key_value),
        )
        override_mapping[key_name] = key_value

    print(_config_table)
    # Parse the JSON into a Python dictionary
    for key, value in override_mapping.items():
        # Split the key into individual keys
        keys = key.split(".")
        # Traverse through the nested dictionaries and update the value
        temp_dict = install_properties
        for k in keys[:-1]:
            temp_dict = temp_dict[k]

        temp_dict[keys[-1]] = value

    return install_properties


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create SOCA installer. Visit https://awslabs.github.io/scale-out-computing-on-aws-documentation/documentation/01-install-soca-cluster/ if you need help"
    )

    parser.add_argument(
        "--cdk-cloudformation-execution-policies",
        "--cloudformation-execution-policies",
        type=str,
        help="AWS CDK CloudFormation Execution Policy ARNs",
    )

    parser.add_argument(
        "--cdk-role-arn", type=str, help="AWS CDK CloudFormation Execution Role ARN"
    )

    parser.add_argument(
        "--cdk-bootstrap-kms-key-id",
        "--cdk-bs-kms-id",
        type=str,
        help="AWS CDK Bootstrap KMS Key ID",
    )

    parser.add_argument(
        "--cdk-custom-permissions-boundary",
        type=str,
        help="AWS CDK Custom Permissions Boundary",
    )

    parser.add_argument(
        "--cdk-termination-protection",
        type=bool,
        help="AWS CDK Termination Protection setting",
    )

    parser.add_argument(
        "--profile",
        "-p",
        type=str,
        help="AWS CLI profile to use. See https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-profiles.html",
    )
    parser.add_argument(
        "--region",
        "-r",
        type=str,
        help="AWS region where you want to deploy your SOCA environment.",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=f"{os.path.dirname(os.path.realpath(__file__))}/../../default_config.yml",
        help="Path of custom config file(s). Defaults to default_config.yml .",
    )
    parser.add_argument(
        "--region-map",
        type=str,
        action="append",
        default=[
            f"{os.path.dirname(os.path.realpath(__file__))}/../../region_map.yml",
            f"{os.path.dirname(os.path.realpath(__file__))}/../../region_map_govcloud.yml",
            f"{os.path.dirname(os.path.realpath(__file__))}/../../region_map_local.yml",
        ],
        help="Path of AMI region mapping files. Defaults to various region_map files.",
    )
    parser.add_argument("--bucket", "-b", type=str, help="S3 Bucket to use")
    parser.add_argument("--ssh-keypair", "-ssh", type=str, help="SSH key to use")
    parser.add_argument("--custom-ami", "-ami", type=str, help="Specify a custom image")
    parser.add_argument(
        "--vpc-cidr",
        "-cidr",
        type=str,
        help="What CIDR do you want to use for your VPC (eg: 10.0.0.0/16)",
    )
    parser.add_argument(
        "--client-ip",
        "-ip",
        type=str,
        help="Client IP authorized to access SOCA on TCP ports 22/443",
    )
    parser.add_argument(
        "--prefix-list-id",
        "-pl",
        type=str,
        help="Prefix list ID with IPs authorized to access SOCA on port 22/443",
    )
    parser.add_argument(
        "--name",
        "-n",
        type=str,
        help="Friendly name for your SOCA cluster. Must be unique in the region. SOCA will be added as prefix",
    )
    parser.add_argument(
        "--override",
        type=str,
        action="append",
        nargs="+",
        help="Configuration key(s) to override. Syntax is '<key_name>,<type>,<value>'. You can use multiple --override if needed.",
    )
    parser.add_argument(
        "--base-os",
        "-os",
        choices=[
            "amazonlinux2",
            "amazonlinux2023",
            "centos7",
            "rocky8",
            "rocky9",
            "rhel7",
            "rhel8",
            "rhel9",
            "ubuntu2204",
            "ubuntu2404",
        ],
        type=str,
        help="The preferred Linux distribution for the controller and compute instances",
    )
    parser.add_argument(
        "--fs-apps-provider",
        dest="fs_apps_provider",
        choices=[
            "efs",
            "fsx_ontap",
            "fsx_lustre",
        ],
        type=str,
        help="Storage Provider to specify for /apps",
    )

    parser.add_argument(
        "--fs-data-provider",
        dest="fs_data_provider",
        choices=[
            "efs",
            "fsx_ontap",
            "fsx_lustre",
        ],
        type=str,
        help="Storage Provider to specify for /data",
    )

    parser.add_argument(
        "--debug",
        action="store_const",
        const=True,
        default=False,
        help="Enable CDK debug mode",
    )
    parser.add_argument(
        "--cdk-cmd",
        type=str,
        choices=[
            "deploy",
            "create",
            "update",
            "ls",
            "list",
            "synth",
            "synthesize",
            "destroy",
            "bootstrap",
        ],
        default="deploy",
    )
    parser.add_argument(
        "--skip-config-message",
        action="store_const",
        const=True,
        default=False,
        help="Skip default_config message",
    )
    parser.add_argument(
        "--format",
        type=str,
        choices=["json"],
        help="Output CfnOutputs via a text file",
    )

    args = parser.parse_args()

    # Use script location as current working directory
    _install_directory = os.path.dirname(os.path.realpath(__file__))
    build_lambda_dependency(install_directory=_install_directory)
    os.chdir(path=_install_directory)

    # Append Solution ID to Boto3 Construct
    aws_solution_user_agent = {"user_agent_extra": "AwsSolution/SO0072/25.3.0"}
    boto_extra_config = config.Config(**aws_solution_user_agent)

    splash_info = f"""
            [red]_____[bright_blue] ____  [magenta]______[yellow]___
           [red]/ ___/[bright_blue]/ __ \\\\[magenta]/ ____[yellow]/   |
           [red]\\__ \\\\[bright_blue]/ / / [magenta]/ /   [yellow]/ /| |
          [red]___/[bright_blue] / /_/ [magenta]/ /___[yellow]/ ___ |
         [red]/____/[bright_blue]\\____/[magenta]\\____[yellow]/_/  |_|
        [red]Scale-[bright_blue]Out [magenta]Computing on [yellow]AWS[default]
    ================================
    > Documentation: https://awslabs.github.io/scale-out-computing-on-aws-documentation/
    > Source Code: https://github.com/awslabs/scale-out-computing-on-aws/
    """

    logger.info(splash_info)

    install_phases = {
        "cluster_name": "Please provide a cluster name ('soca-' is automatically added as a prefix)",
        "bucket": "Enter the name of an S3 bucket you own",
        "baseos": "Choose the default operating system (this can be changed later for the compute nodes)",
        "key_pair": "Choose the SSH keypair to use",
        "vpc": "Do you want to create new resources (default) or use existing resources?",
        "security_groups": "Do you want to create new security groups (default) or use existing ones? ",
        "filesystems": "Do you want to create new filesystems for /apps & /data (default) or use existing ones? ",
        "directory_service": "Do you want to create a new Directory Service Managed AD (default) or use an existing one? ",
        "analytics": "Do you want to use an existing Analytics back-end (OpenSearch)?",
        "iam_roles": "Do you want to create new IAM roles for controller & compute nodes (default) or use existing ones?",
        "apps_storage_provider": "What storage provider do you want to use for your /apps partition? (fsx_ontap is recommended if you have Linux and Windows clients)",
        "data_storage_provider": "What storage provider do you want to use for your /data partition? (fsx_ontap is recommended if you have Linux and Windows clients)",
    }

    install_parameters = {
        # SOCA parameters
        "base_os": None,
        "account_id": None,
        "bucket": None,
        "ssh_keypair": None,
        "cluster_name": None,
        "cluster_id": None,
        "custom_ami": None,
        "region": None,
        "client_ip": None,
        # Network
        "vpc_id": None,
        "vpc_azs": None,
        "public_subnets": None,
        "private_subnets": None,
        # Filesystem for /data and /apps
        "fs_apps_provider": None,
        "fs_apps": None,
        "fs_data_provider": None,
        "fs_data": None,
        # AWS Directory Service Managed AD
        "directory_service_user": None,
        "directory_service_user_password": None,
        "directory_service_shortname": None,
        "directory_service_name": None,
        "directory_service_id": None,
        "directory_service_dns": None,
        # EC2 Security Groups
        "compute_node_sg": None,
        "controller_sg": None,
        "alb_sg": None,
        "login_node_sg": None,
        "nlb_sg": None,
        # IAM role
        "compute_node_role_name": None,
        "compute_node_role_arn": None,
        "computenode_role_from_previous_soca_deployment": None,
        "controller_role_name": None,
        "controller_role_arn": None,
        "controller_role_from_previous_soca_deployment": None,
        "spotfleet_role_name": None,
        "spotfleet_role_arn": None,
        "spotfleet_role_from_previous_soca_deployment": None,
        # ElasticSearch
        "es_domain": None,
    }
    logger.info("\n====== Validating Default SOCA Configuration ======\n")

    # Read in the configuration file(s) specified by the user
    logger.debug(f"Config file(s) specified: {args.config}")

    install_properties = get_install_properties(pathname=args.config)

    logger.debug(f"Install properties after file read: {install_properties}")

    # Read in the RegionMap file(s) specified by the user
    logger.debug(f"RegionMap file(s) specified: {type(args.region_map)} / {args.region_map=}")

    _region_map: dict = {}

    # If we are a string, convert to a list[str] of the single member
    if isinstance(args.region_map, str):
        logger.info(f"Converting string RegionMap to list member")
        args.region_map = [args.region_map]

    if isinstance(args.region_map, list):
        logger.info(f"Reading {len(args.region_map)} RegionMap files")
        for _file in args.region_map:
            logger.info(f"Reading RegionMap file: {os.path.basename(_file)}")
            if not os.path.isfile(_file):
                # This isn't an error yet
                logger.info(f"RegionMap file not found: {_file}")
                continue

            # We specifically look in "RegionMap" to make sure it is well-formed YAML
            # and to get an accurate count to see if it has any content
            _region_map_contents: dict = get_install_properties(pathname=_file).get("RegionMap", {})

            # logger.debug(f"RegionMap contents: {_region_map_contents}")
            if len(_region_map_contents) == 0:
                logger.info(f"RegionMap file is empty or malformed: {_file}")
                continue
            else:
                logger.debug(f"Content len {len(_region_map_contents)}")
                _region_map.update(_region_map_contents)
                logger.debug(f"RegionMap after applying file update: {_region_map}")
    else:
        logger.error(f"RegionMap is not a list. Exiting.")
        sys.exit(1)

    logger.debug(f"RegionMap after all files are read: {_region_map}")

    if args.override:
        overrides: list = [item for sublist in args.override for item in sublist]
        install_properties = override_keys(overrides, install_properties)

    _merged_properties: dict = {**install_properties, "RegionMap": _region_map}

    logger.debug(f"Merged properties: {_merged_properties}")

    install_parameters["install_properties"] = base64.b64encode(
        json.dumps(_merged_properties).encode("utf-8")
    ).decode("utf-8")

    install_props = json.loads(
        json.dumps(_merged_properties),
        object_hook=lambda d: SimpleNamespace(**d),
    )

    logger.debug(f"Install props after override processing: {install_props}")

    if not args.skip_config_message:
        if (
            get_input(
                prompt=f"SOCA will create AWS resources using the default parameters specified on installer/default_config.yml. \n Make sure you have read, reviewed and updated them (if needed). Enter 'yes' to continue ...",
                specified_value=None,
                expected_answers=["yes", "no"],
                expected_type=str,
            )
            != "yes"
        ):
            sys.exit(1)

    logger.info("\n====== Validating AWS Environment ======\n")

    # Load AWS custom profile if specified on CLI or env var
    #
    _profile_to_use: str = ""
    _env_profile: str = os.environ.get("AWS_PROFILE", "")
    logger.debug(f"AWS profile from environment (AWS_PROFILE): {_env_profile}")

    _profile_to_use = _env_profile if _env_profile else ""

    # The CLI over-rides the ENV - give a warning to the user
    if args.profile:
        if _env_profile:
            logger.warning(
                f"Using AWS profile sourced from CLI (but also found ENV var): {_profile_to_use}  (ENV: {_env_profile})"
            )
        _profile_to_use = args.profile

    try:
        if _profile_to_use:
            session = boto3.session.Session(profile_name=_profile_to_use)
        else:
            logger.debug(f"Using plain Boto3 session call")
            session = boto3.session.Session()
    except ProfileNotFound:
        logger.error(f"Profile {args.profile} not found. Check ~/.aws/credentials file")
        sys.exit(1)

    # Determine our partition from calling STS client (without any specific region info)
    _sts_client = session.client("sts")
    _sts_caller_arn: str = _sts_client.get_caller_identity().get("Arn", "")

    if not _sts_caller_arn:
        logger.error("Unable to determine AWS partition via STS. Exiting...")
        sys.exit(1)

    _sts_partition: str = ""
    try:
        _sts_partition = _sts_caller_arn.split(":")[1]
    except IndexError as err:
        logger.error(
            f"Unable to determine AWS partition via STS. Error: {err}. Exiting..."
        )

    logger.info(f"STS-discovered caller ARN: {_sts_caller_arn}")
    logger.info(f"STS-discovered AWS partition: {_sts_partition}")

    install_parameters["partition"] = _sts_partition

    # Determine all AWS regions available on the account. We do not display opt-out regions
    # This uses us-east-1 as a default probe destination
    # For alternate partitions (e.g. GovCloud) - the AWS_DEFAULT_REGION MUST be set prior
    # to running the installer else we will not be able to enumerate the regions in the partition.

    # Set our default region based on the partition
    # Add additional partitions here
    default_region: str = ""
    _ssm_region_query: bool = False

    logger.debug(f"Default region: {default_region} / {_ssm_region_query=}")
    match _sts_partition:
        case "aws":
            default_region = "us-east-1"
            _ssm_region_query = True
        case "aws-us-gov":
            default_region = "us-gov-west-1"
        case "aws-cn":
            default_region = "cn-north-1"
        case _:
            default_region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    logger.info(f"Default region based on STS partition: {default_region}")
    ec2 = session.client("ec2", region_name=default_region, config=boto_extra_config)
    if _ssm_region_query:
        ssm_client = session.client(
            "ssm", region_name=default_region, config=boto_extra_config
        )

    try:
        # describe_regions does not support pagination 11 Oct 2024
        accepted_regions = [
            _region.get("RegionName")
            for _region in ec2.describe_regions().get("Regions")
        ]
    except ClientError as err:
        logger.warning(
            f"Unable to list all AWS regions, you will need to enter it manually or give ec2:Describe* IAM permission. {err} "
        )
        accepted_regions = []

    if not accepted_regions:
        logger.error("No AWS regions found. Exiting...")
        sys.exit(1)

    # Build a dict of the accepted_regions
    # TODO - Make a table with a better layout versus generic listing
    accepted_regions_dict: dict = defaultdict(dict)
    _region_extra_data: dict = defaultdict(dict)

    _ssm_param_list: list = []
    for _region in accepted_regions:
        if _region == default_region:
            accepted_regions_dict[_region]["default"] = True
            accepted_regions_dict[_region]["visible"] = True
        else:
            accepted_regions_dict[_region]["default"] = False
            accepted_regions_dict[_region]["visible"] = True
        if _ssm_region_query:
            _ssm_param_list.append(
                f"/aws/service/global-infrastructure/regions/{_region}/longName"
            )
            _ssm_param_list.append(
                f"/aws/service/global-infrastructure/regions/{_region}/geolocationCountry"
            )
    #        _ssm_param_list.append(f"/aws/service/global-infrastructure/regions/{_region}/partition")

    if _ssm_region_query:
        logger.debug(f"SSM Param lookup length: {len(_ssm_param_list)}")
        # Chunk the regions into 10 param lookups
        # _ssm_parm_lists is a list of lists with 10 entries ea
        _ssm_param_lists = [
            _ssm_param_list[i : i + 10] for i in range(0, len(_ssm_param_list), 10)
        ]
        logger.debug(
            f"SSM Param Lookup Chunks needed (10 values per chunk): {len(_ssm_param_lists)}"
        )

        _ssm_chunk_i: int = 1
        for _i in _ssm_param_lists:
            logger.debug(f"Processing SSM Param chunk #{_ssm_chunk_i}")
            _ssm_reply_values = ssm_client.get_parameters(Names=_i).get(
                "Parameters", []
            )
            if _ssm_reply_values:
                for _region_entry in _ssm_reply_values:
                    # store what we want - paths are like /aws/service/global-infrastructure/regions/us-east-1/longName
                    _region_data_key: str = _region_entry.get("Name").split("/")[
                        -1
                    ]  # e.g. longName
                    _region_name: str = _region_entry.get("Name").split("/")[
                        -2
                    ]  # e.g. us-east-1
                    _region_data_value: str = _region_entry.get("Value", "")

                    logger.debug(
                        f"Resolved region data for {_region_name} - {_region_data_key} => ({_region_data_value})"
                    )
                    _region_extra_data[_region_name][
                        _region_data_key
                    ] = _region_data_value

            _ssm_chunk_i += 1

    logger.debug(f"Available regions: {accepted_regions_dict=} / {_region_extra_data}")

    _region_table = Table(show_header=True, header_style="bold")
    _region_table.add_column(header="AWS Region Code", width=18, justify="center")
    _region_table.add_column(header="Country\nCode", width=9, justify="center")
    _region_table.add_column(header="Description", width=35, justify="center")
    #    _region_table.add_column(header="Partition", width=12, justify="center")

    # populate the table
    for _region in sorted(accepted_regions_dict):
        _region_table.add_row(
            _region,
            (
                _region_extra_data[_region]["geolocationCountry"]
                if _ssm_region_query
                else _region.split("-")[0].upper()
            ),
            _region_extra_data[_region]["longName"] if _ssm_region_query else _region,
            #            _region_extra_data[_region]["partition"] if _ssm_region_query else _region,
        )

    _region_table = Align.center(_region_table, vertical="middle")
    print(_region_table)

    # Choose region where to install SOCA
    install_parameters["region"] = get_input(
        prompt=f"What AWS region do you want to install SOCA? (e.g. {default_region})",
        specified_value=args.region,
        expected_answers=accepted_regions_dict,
        expected_type=str,
        show_default_answer=True,
        show_expected_answers=False,
    )

    # Initiate boto3 clients now the partition and region is known
    # TODO - there are better ways to do this

    secretsmanager = session.client(
        "secretsmanager",
        region_name=install_parameters["region"],
        config=boto_extra_config,
    )

    ec2 = session.client(
        "ec2", region_name=install_parameters["region"], config=boto_extra_config
    )
    sts = session.client(
        "sts", region_name=install_parameters["region"], config=boto_extra_config
    )
    s3 = session.resource(
        "s3", region_name=install_parameters["region"], config=boto_extra_config
    )
    efs = session.client(
        "efs", region_name=install_parameters["region"], config=boto_extra_config
    )
    fsx = session.client(
        "fsx", region_name=install_parameters["region"], config=boto_extra_config
    )
    ssm = session.client(
        "ssm", region_name=install_parameters["region"], config=boto_extra_config
    )
    cloudformation = session.client(
        "cloudformation",
        region_name=install_parameters["region"],
        config=boto_extra_config,
    )
    iam = session.client(
        "iam", region_name=install_parameters["region"], config=boto_extra_config
    )
    kms = session.client(
        "kms", region_name=install_parameters["region"], config=boto_extra_config
    )
    # Perform KMS key alias check to pre-generate service default KMS key Aliases if needed
    kms_prepare_account_aliases()

    accepted_aws_values = accepted_aws_resources(region=install_parameters["region"])

    # Verify if we have the default Service Linked Role for ElasticSearch. SOCA will create it if needed
    install_parameters["create_es_service_role"] = False
    try:
        logger.debug(f"Validating OpenSearch SLR")
        _iam_paginator = iam.get_paginator("list_roles")
        _iam_iter = _iam_paginator.paginate(
            PathPrefix="/aws-service-role/opensearchservice.amazonaws.com"
        )
        for _page in _iam_iter:
            logger.debug(f"Processing Role page: {_page}")
            if not _page.get("Roles", []):
                install_parameters["create_es_service_role"] = True
                break

    except ClientError as err:
        logger.error(
            f"Unable to determine if you have a ServiceLinked Role created on your account for OpenSearch. Verify your IAM permissions: {err} "
        )
        sys.exit(1)

    # Retrieve the AWS Account ID for CDK
    try:
        install_parameters["account_id"] = sts.get_caller_identity()["Account"]
    except Exception as err:
        logger.error(f"Unable to retrieve the Account ID due to {err}")
        sys.exit(1)

    # Automatically detect client ip if needed
    if not args.client_ip:
        install_parameters["client_ip"] = detect_customer_ip()

        if install_parameters["client_ip"]:
            logger.warning(
                f"We determined your IP is {install_parameters['client_ip']}. You can change it later if you are running behind a proxy"
            )
        else:
            logger.warning(
                f"Unable to automatically determine your IP address. Manual specification will be required"
            )
    else:
        install_parameters["client_ip"] = args.client_ip

    # If we had to auto-probe the IP , give the option to update it before continuing
    install_parameters["client_ip"] = get_input(
        "Client IP/CIDR authorized to access SOCA on TCP ports 443/22",
        specified_value=install_parameters["client_ip"],
        expected_answers=None,
        expected_type=str,
    )

    if "/" not in install_parameters["client_ip"]:
        logger.warning(
            f"No subnet/CIDR defined for your IP. Adding /32 at the end of {install_parameters['client_ip']}"
        )
        install_parameters["client_ip"] = f"{install_parameters['client_ip']}/32"



    # Get SOCA parameters
    get_install_parameters()

    # Validate Config, relaunch installer if needed
    while validate_soca_config(install_parameters, install_props) is False:
        get_install_parameters()

    # Validate CloudFormation stack name
    try:
        check_if_name_exist = cloudformation.describe_stacks(
            StackName=install_parameters["cluster_id"]
        )
        if len(check_if_name_exist["Stacks"]) != 0:
            if args.cdk_cmd == "create":
                logger.error(
                    f"{install_parameters['cluster_id']} already exists in CloudFormation. Please pick a different name and try again (soca- is automatically added as a prefix)."
                )
                sys.exit(1)
            elif args.cdk_cmd == "deploy":
                logger.error(
                    f"{install_parameters['cluster_id']} already exists in CloudFormation. Use --cdk-cmd update if you want to update it."
                )
                sys.exit(1)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ValidationError":
            if args.cdk_cmd == "update":
                logger.error(
                    f"{install_parameters['cluster_id']} does not exist in CloudFormation so can't be updated. Use --cdk-cmd deploy if you want to create it."
                )
                sys.exit(1)
            else:
                # Stack does not exist so create it
                pass
        else:
            logger.error(
                f"Error checking if {install_parameters['cluster_id']} already exists in CloudFormation due to {e}."
            )
            sys.exit(1)

    # Prepare CDK commands
    # Start to build up some common CDK args in case we need them
    # Make sure to start the string with a leading space

    _cdk_common_args: str = (
        f"--output cdk.out/{install_parameters['cluster_id']}/{install_parameters['region']}"
    )

    # User has requested specific CDK Policy ARN(s)
    if args.cdk_cloudformation_execution_policies:
        _cdk_common_args += f" --cloudformation-execution-policies {args.cdk_cloudformation_execution_policies}"

    # User has requested a specific CDK Role ARN
    if args.cdk_role_arn:
        _cdk_common_args += f" --role-arn {args.cdk_role_arn}"

    # User has requested a specific KMS KeyID to used
    if args.cdk_bootstrap_kms_key_id:
        _cdk_common_args += f" --bootstrap-kms-key-id {args.cdk_bootstrap_kms_key_id}"

    if args.cdk_custom_permissions_boundary:
        _cdk_common_args += (
            f" --custom-permissions-boundary {args.cdk_custom_permissions_boundary}"
        )

    # Default is to enable termination protection as a best practice
    _cdk_common_args += " --termination-protection"
    logger.info(":lock: [green]CDK Bootstrap Stack Termination protection is enabled")

    if args.debug:
        _cdk_common_args += " --debug -v -v -v"

    if args.profile:
        _cdk_common_args += f" --profile {args.profile}"
        install_parameters["profile"] = "False" if not args.profile else args.profile

    if args.cdk_cmd in ["create", "update"]:
        cdk_cmd = "deploy"
    else:
        cdk_cmd = args.cdk_cmd

    cmd = f"cdk {cdk_cmd} {_cdk_common_args} -c {' -c '.join('{}={}'.format(key,val) for (key,val) in install_parameters.items() if val is not None)} --require-approval never"
    cmd_bootstrap = f"cdk bootstrap {_cdk_common_args} aws://{install_parameters['account_id']}/{install_parameters['region']} -c {' -c '.join('{}={}'.format(key,val) for (key,val) in install_parameters.items() if val is not None)}"

    # Adding --debug flag will output the cdk deploy command. This is helpful for troubleshooting.
    if args.debug:
        logger.debug(f"\nExecuting {cmd}")

    # Log command in history book
    with open("installer_history.txt", "a+") as f:
        f.write(
            f"""\n==== [{datetime.datetime.now(datetime.UTC)}] ====
{cmd}
{str(install_parameters)}
============================="""
        )

    # First, Bootstrap the environment. This will create a staging S3 bucket if needed
    logger.info("\n====== Running CDK Bootstrap ======\n")

    bootstrap_environment = subprocess.run(shlex.split(cmd_bootstrap), stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # nosec

    if int(bootstrap_environment.returncode) != 0:
        logger.error(
            f"Unable to bootstrap environment. Please run {cmd_bootstrap} and fix any errors"
        )
        logger.error(f"{bootstrap_environment} ")
        sys.exit(1)

    # Increase SSM Throughput if needed (https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-store-throughput.html)
    # Settings will be restored if needed post deployment
    disable_ssm_high_throughput_post_install: bool = False

    # Upload required assets to customer S3 account
    if cdk_cmd == "deploy":
        upload_objects(
            _install_directory,
            install_parameters["bucket"],
            install_parameters["cluster_id"],
        )

        _check_ssm_high_throughput = ssm.get_service_setting(
            SettingId="/ssm/parameter-store/high-throughput-enabled"
        )

        if (
            _check_ssm_high_throughput.get("ServiceSetting").get("SettingValue")
            == "false"
        ):
            logger.warning(
                "Temporarily enabling /ssm/parameter-store/high-throughput-enabled for SOCA deployment"
            )
            # try/catch
            # Validate the update
            ssm.update_service_setting(
                SettingId="/ssm/parameter-store/high-throughput-enabled",
                SettingValue="true",
            )
            disable_ssm_high_throughput_post_install = True

    # Then launch the actual SOCA installer
    logger.info("\n====== Deploying SOCA ======\n")
    launch_installer = os.system(cmd)  # nosec

    if cdk_cmd == "deploy":
        # Optional - Re-enable SSM default
        if disable_ssm_high_throughput_post_install:
            logger.warning(
                "Restoring /ssm/parameter-store/high-throughput-enabled to its previous value post-deployment"
            )
            # try/catch
            # validate
            ssm.update_service_setting(
                SettingId="/ssm/parameter-store/high-throughput-enabled",
                SettingValue="false",
            )

        if int(launch_installer) == 0:
            # SOCA is installed. We will now wait until SOCA is fully configured (when the ELB returns HTTP 200)
            logger.info(f"[bold green]SOCA was installed successfully![/bold green]")

            if install_props.Config.directoryservice.provider not in [
                "existing_openldap",
                "existing_active_directory",
            ]:
                _get_admin_password = retrieve_secret_value(
                    secret_id="/soca/"
                    + install_parameters["cluster_id"]
                    + "/SocaAdminUser"
                )

                logger.info(f"{'=' * 44}")
                _auth_table = Table(
                    title=f"SOCA Default Admin Credentials",
                    show_lines=True,
                    highlight=True,
                )
                _auth_table.add_column(
                    header="[bold yellow]Default Username[/bold yellow]",
                    justify="center",
                    no_wrap=True,
                )
                _auth_table.add_column(
                    header="[bold yellow]Default Password[/bold yellow]",
                    justify="center",
                    no_wrap=True,
                )
                _auth_table.add_row(
                    _get_admin_password.get("username"),
                    _get_admin_password.get("password"),
                )
                Console(emoji=False).print(_auth_table)
                logger.info(
                    f"[bold green]Use the login/password credential to log in when your endpoint is ready."
                )
                logger.info(f"{'=' * 44}")
            else:
                logger.info(f"{'=' * 44}")
                logger.info(
                    f"[bold green]Using an existing Active Directory or OpenLDAP. Use an existing user to log in."
                )
                logger.info(f"{'=' * 44}")

            try:
                check_cfn = cloudformation.describe_stacks(
                    StackName=install_parameters["cluster_id"]
                )
                if args.format == "json":
                    with open(
                        f"{install_parameters['cluster_id']}.output", "w"
                    ) as outfile:
                        json.dump(check_cfn["Stacks"][0]["Outputs"], outfile)

                for output in check_cfn["Stacks"][0]["Outputs"]:
                    if output["OutputKey"] == "WebUserInterface":
                        logger.info(
                            f"SOCA Web Endpoint is {output['OutputValue']} . Now checking if SOCA is fully configured (this could take up to 30 minutes)"
                        )
                        # Run a first check to determine if client IP provided by the customer is valid
                        try:
                            check_firewall = get(
                                f"{output['OutputValue']}", verify=False, timeout=35
                            )  # nosec
                        except Timeout:
                            logger.warning(
                                f"Unable to connect to the SOCA endpoint URL. Maybe your IP {install_parameters['client_ip']} is not valid/has changed (maybe you are behind a proxy?). If that's the case please go to AWS console and authorize your real IP address on the ALB and NLB Security Groups"
                            )
                            sys.exit(1)
                        except ConnectionError as e:
                            logger.warning(
                                f"Encountered ConnectionError. Unable to connect to the SOCA endpoint URL. Error: {e} "
                            )
                        except ConnectionRefusedError as e:
                            logger.warning(
                                f"Encountered ConnectionRefusedError. Unable to connect to the SOCA endpoint URL. Error: {e} "
                            )

                        soca_check_loop = 0
                        if install_parameters["vpc_id"]:
                            # SOCA deployment is shorter when using existing resources, so we increase the timeout
                            max_check_loop = 15
                        else:
                            max_check_loop = 10
                        # print(f"DEBUG - Starting Endpoint check loop - MaxCheckLoop: {max_check_loop}")
                        while (
                            get(
                                output["OutputValue"], verify=False, timeout=15  # nosec
                            ).status_code
                            != 200
                            and soca_check_loop <= max_check_loop
                        ):  # nosec
                            logger.info(
                                "SOCA not ready yet, checking again in 300 seconds ... "
                            )
                            time.sleep(300)
                            soca_check_loop += 1
                            if soca_check_loop >= max_check_loop:
                                logger.warning(
                                    f"Could not determine if SOCA is ready after {max_check_loop*2} minutes. Connect to the system via SSM and check the logs. "
                                )
                                sys.exit(1)

                        # at this point SOCA Web UI is fully operational, however the user creation will happen within the next 20 to 40 seconds.
                        # Adding extra delay to be sure the user is created and customer can click and login.

                        time.sleep(120)
                        logger.info(
                            f"[green]SOCA is ready! Login via {output['OutputValue']}"
                        )

            except ValidationError:
                logger.error(
                    f"{install_parameters['cluster_id']} is not a valid cloudformation stack"
                )
            except ClientError as err:
                logger.error(
                    f"Unable to retrieve {install_parameters['cluster_id']} stack outputs, probably due to a permission error (your IAM account do not have permission to run cloudformation:Describe*. Log in to AWS console to view your stack connection endpoints"
                )

    elif args.cdk_cmd == "destroy":
        # Destroy stack if known
        cmd_destroy = f"cdk destroy {install_parameters['cluster_id']} -c {' -c '.join('{}={}'.format(key, val) for (key, val) in install_parameters.items() if val is not None)} --require-approval never"
        logger.info(f"Deleting stack, running {cmd_destroy}")
        delete_stack = os.system(cmd_destroy)  # nosec
    else:
        # synth, ls etc.
        pass
