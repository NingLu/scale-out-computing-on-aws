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

import logging
import boto3
import config
from flask import render_template, Blueprint, request, redirect, session, flash
from decorators import login_required, admin_only
from utils.http_client import SocaHttpClient

logger = logging.getLogger("soca_logger")
admin_virtual_desktops_software_stacks = Blueprint(
    "software_stacks", __name__, template_folder="templates"
)


def get_region():
    session = boto3.session.Session()
    aws_region = session.region_name
    return aws_region


@admin_virtual_desktops_software_stacks.route(
    "/admin/virtual_desktops/software_stacks", methods=["GET"]
)
@login_required
@admin_only
def index():
    logger.info(f"List all DCV images registered to SOCA")
    _list_software_stacks = SocaHttpClient(
        endpoint="/api/dcv/virtual_desktops/software_stacks",
        headers={"X-SOCA-USER": session["user"], "X-SOCA-TOKEN": session["api_key"]},
    ).get()

    _list_profiles = SocaHttpClient(
        endpoint="/api/dcv/virtual_desktops/profiles",
        headers={"X-SOCA-USER": session["user"], "X-SOCA-TOKEN": session["api_key"]},
    ).get()

    if _list_software_stacks.get("success") is True:
        return render_template(
            "admin/virtual_desktops/software_stacks.html",
            user=session["user"],
            profiles=_list_profiles.get("message"),
            supported_base_os=config.Config.DCV_BASE_OS.keys(),
            software_stacks=_list_software_stacks.get("message"),
            region_name=get_region(),
        )
    else:
        flash(
            f"Unable to list SOCA Software Stacks because of {_list_software_stacks.get('message')}",
            "error",
        )
        return render_template(
            "admin/virtual_desktops/software_stacks.html",
            user=session["user"],
            profiles={},
            software_stacks={},
            region_name=get_region(),
        )


@admin_virtual_desktops_software_stacks.route(
    "/admin/virtual_desktops/software_stacks/create", methods=["POST"]
)
@login_required
@admin_only
def software_stack_create():
    logger.info(
        f"Received following parameters {request.form} to create software stack image"
    )
    _form_data = request.form.to_dict()
    _thumbnail = request.files.get("thumbnail")
    _files = (
        {"thumbnail": (_thumbnail.filename, _thumbnail.stream, _thumbnail.mimetype)}
        if _thumbnail
        else None
    )
    _create_software_stack = SocaHttpClient(
        endpoint="/api/dcv/virtual_desktops/software_stacks",
        headers={"X-SOCA-USER": session["user"], "X-SOCA-TOKEN": session["api_key"]},
    ).post(data=request.form.to_dict(), files=_files)

    if _create_software_stack.get("success") is True:
        flash(
            f"Your software stack {_create_software_stack.get('message')} has been registered successfully. Add it to at least one of your SOCA project before being able to use it.",
            "success",
        )
    else:
        flash(
            f"Unable to register your image because of {_create_software_stack.get('message')}",
            "error",
        )

    return redirect("/admin/virtual_desktops/software_stacks")


@admin_virtual_desktops_software_stacks.route(
    "/admin/virtual_desktops/software_stacks/delete", methods=["POST"]
)
@login_required
@admin_only
def software_stack_delete():
    logger.info(
        f"Received following parameters {request.form} to create software stack image"
    )
    logger.info(f"Received following parameters {request.form} to delete DCV image")

    _delete_software_stack = SocaHttpClient(
        endpoint="/api/dcv/virtual_desktops/software_stacks",
        headers={"X-SOCA-USER": session["user"], "X-SOCA-TOKEN": session["api_key"]},
    ).delete(data=request.form.to_dict())

    if _delete_software_stack.get("success") is True:
        flash(
            f"Your software stack has been removed  successfully",
            "success",
        )
    else:
        flash(
            f"Unable to remove your software stack because of {_delete_software_stack.get('message')}",
            "error",
        )

    return redirect("/admin/virtual_desktops/software_stacks")


@admin_virtual_desktops_software_stacks.route(
    "/admin/virtual_desktops/software_stacks/edit", methods=["POST", "GET"]
)
@login_required
@admin_only
def software_stack_edit():
    if request.method == "GET":
        return redirect("/admin/virtual_desktops/software_stacks")

    logger.info(
        f"Received following parameters {request.form} to edit software stack image"
    )
    _software_stack_to_modify = request.form.get("software_stack_id", None)
    if _software_stack_to_modify is None:
        flash("Missing software_stack_id", "error")
        return redirect("/admin/virtual_desktops/software_stacks")

    _get_software_stack_info = SocaHttpClient(
        endpoint="/api/dcv/virtual_desktops/software_stacks",
        headers={"X-SOCA-USER": session["user"], "X-SOCA-TOKEN": session["api_key"]},
    ).get(params={"software_stack_id": request.form.get("software_stack_id")})

    _list_profiles = SocaHttpClient(
        endpoint="/api/dcv/virtual_desktops/profiles",
        headers={"X-SOCA-USER": session["user"], "X-SOCA-TOKEN": session["api_key"]},
    ).get()

    if _get_software_stack_info.get("success") is True:
        return render_template(
            "admin/virtual_desktops/software_stacks_edit.html",
            user=session["user"],
            profiles=_list_profiles.get("message"),
            supported_base_os=config.Config.DCV_BASE_OS.keys(),
            software_stack_info=_get_software_stack_info.get("message").get(
                _software_stack_to_modify
            ),
            region_name=get_region(),
        )
    else:
        flash(
            f"Unable to list SOCA Software Stacks because of {_get_software_stack_info.get('message')}",
            "error",
        )
        return redirect("/admin/virtual_desktops/software_stacks")


@admin_virtual_desktops_software_stacks.route(
    "/admin/virtual_desktops/software_stacks/edit/update", methods=["POST"]
)
@login_required
@admin_only
def software_stack_update():
    logger.info(
        f"Received following parameters {request.form} to edit software stack image"
    )
    _thumbnail = request.files.get("thumbnail", None)
    _files = (
        {"thumbnail": (_thumbnail.filename, _thumbnail.stream, _thumbnail.mimetype)}
        if _thumbnail
        else None
    )
    _software_stack_to_modify = request.form.get("software_stack_id", None)
    if _software_stack_to_modify is None:
        flash("Missing software_stack_id", "error")
        return redirect("/admin/virtual_desktops/software_stacks")

    _modify_software_stack = SocaHttpClient(
        endpoint="/api/dcv/virtual_desktops/software_stacks",
        headers={"X-SOCA-USER": session["user"], "X-SOCA-TOKEN": session["api_key"]},
    ).put(data=request.form.to_dict(), files=_files)

    if _modify_software_stack.get("success") is True:
        flash(
            f"Your Software Stack has been updated successfully",
            "success",
        )
    else:
        flash(
            f"{_modify_software_stack.get('message')}",
            "error",
        )
    return redirect("/admin/virtual_desktops/software_stacks")
