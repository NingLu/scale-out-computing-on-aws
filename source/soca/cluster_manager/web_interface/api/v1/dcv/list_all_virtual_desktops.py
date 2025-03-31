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

from flask_restful import Resource, reqparse
from flask import request
import logging
from decorators import admin_api
import os
import sys
from models import db, VirtualDesktopSessions, SoftwareStacks, VirtualDesktopProfiles
import utils.aws.boto3_wrapper as utils_boto3
from utils.aws.ssm_parameter_store import SocaConfig
from utils.error import SocaError
from utils.response import SocaResponse


logger = logging.getLogger("soca_logger")

client_ec2 = utils_boto3.get_boto(service_name="ec2").message
client_cfn = utils_boto3.get_boto(service_name="cloudformation").message
client_ssm = utils_boto3.get_boto(service_name="ssm").message


class ListAllVirtualDesktops(Resource):
    @admin_api
    def get(self):
        """
        List all virtual desktops
        """
        parser = reqparse.RequestParser()
        args = parser.parse_args()
        logger.debug(f"Received parameter for listing all DCV desktop: {args}")

        user = request.headers.get("X-SOCA-USER")
        if user is None:
            return SocaError.CLIENT_MISSING_HEADER(header="X-SOCA-USER").as_flask()

        _all_dcv_sessions = (
            VirtualDesktopSessions.query.join(
                SoftwareStacks,
                SoftwareStacks.id == VirtualDesktopSessions.software_stack_id,
            )
            .join(
                VirtualDesktopProfiles,
                VirtualDesktopProfiles.id == SoftwareStacks.virtual_desktop_profile_id,
            )
            .filter(
                VirtualDesktopSessions.is_active == True,
            )
            .add_columns(
                SoftwareStacks.id,
                SoftwareStacks.stack_name,
                SoftwareStacks.ami_id,
                SoftwareStacks.ami_arch,
                VirtualDesktopProfiles.id,
                VirtualDesktopProfiles.profile_name,
            )
        )

        logger.info(f"Found all DCV sessions {_all_dcv_sessions.all()}")

        user_sessions = {}
        logger.info("Getting Session information for all session")

        _get_soca_parameters = SocaConfig(key="/").get_value(return_as=dict)
        if _get_soca_parameters.get("success") is False:
            return SocaError.GENERIC_ERROR(
                helper=f"Unable to retrieve SOCA Parameters: {_get_soca_parameters.get('message')}"
            ).as_flask()
        else:
            _soca_parameters = _get_soca_parameters.get("message")

        for (
            session_info,
            stack_id,
            stack_name,
            ami_id,
            ami_arch,
            profile_id,
            profile_name,
        ) in _all_dcv_sessions.all():
            try:
                _session_data = {
                    "id": session_info.id,
                    "is_active": session_info.is_active,
                    "created_on": session_info.created_on,
                    "deactivated_on": session_info.deactivated_on,
                    "deactivated_by": session_info.deactivated_by,
                    "stack_name": session_info.stack_name,
                    "session_uuid": session_info.session_uuid,
                    "session_id": session_info.session_id,
                    "session_owner": session_info.session_owner,
                    "session_name": session_info.session_name,
                    "session_state": session_info.session_state,
                    "session_token": session_info.session_token,
                    "session_type": session_info.session_type,
                    "session_local_admin_password": session_info.session_local_admin_password,
                    "authentication_token": session_info.authentication_token,
                    "session_thumbnail": session_info.session_thumbnail,
                    "os_family": session_info.os_family,
                    "schedule": session_info.schedule,
                    "instance_private_dns": session_info.instance_private_dns,
                    "instance_private_ip": session_info.instance_private_ip,
                    "instance_id": session_info.instance_id,
                    "instance_type": session_info.instance_type,
                    "instance_base_os": session_info.instance_base_os,
                    "support_hibernation": session_info.support_hibernation,
                    "ami_arch": ami_arch,
                    "ami_id": ami_id,
                    "vdi_profile": f"{profile_name} (id: {profile_id})",
                    "software_stack": f"{stack_name} (id: {stack_id})",
                }

                user_sessions[session_info.session_uuid] = _session_data
                logger.info(f"Session Info {user_sessions[session_info.session_uuid]}")

            except Exception as err:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                return SocaError.GENERIC_ERROR(
                    helper=f"{err}, {exc_type}, {fname}, {exc_tb.tb_lineno}"
                ).as_flask()

        logger.debug(f"Complete User Sessions details to return: {user_sessions}")
        return SocaResponse(success=True, message=user_sessions).as_flask()
