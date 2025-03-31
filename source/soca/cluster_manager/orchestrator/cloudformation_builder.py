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
import base64
import os
import sys
import re

from troposphere import GetAtt
from troposphere import Ref, Template
from troposphere import Tags as base_Tags  # without PropagateAtLaunch
from troposphere.autoscaling import (
    AutoScalingGroup,
    LaunchTemplateSpecification,
    Tags,
    LaunchTemplateOverrides,
    MixedInstancesPolicy,
    InstancesDistribution,
)
from troposphere.autoscaling import LaunchTemplate as asg_LaunchTemplate
from troposphere.cloudformation import AWSCustomObject
from troposphere.ec2 import (
    PlacementGroup,
    BlockDeviceMapping,
    LaunchTemplate,
    LaunchTemplateData,
    MetadataOptions,
    EBSBlockDevice,
    IamInstanceProfile,
    InstanceMarketOptions,
    NetworkInterfaces,
    SpotOptions,
    CpuOptions,
    LaunchTemplateBlockDeviceMapping,
)

from troposphere.fsx import FileSystem, LustreConfiguration
import troposphere.ec2 as ec2

import pathlib

from utils.aws.ssm_parameter_store import SocaConfig
from utils.jinjanizer import SocaJinja2Generator

import logging
import boto3
import uuid

from utils.response import SocaResponse

# FIXME TODO - ExtraConfig / API / versioning?
ec2_client = boto3.client("ec2")
logger = logging.getLogger("soca_logger")


def clean_user_data(text_to_remove: list, data: str) -> str:
    _ec2_user_data = data
    for _t in text_to_remove:
        _ec2_user_data = re.sub(f"{_t}", "", _ec2_user_data, flags=re.IGNORECASE)

    # Remove leading spaces
    _ec2_user_data = re.sub(r"^[ \t]+", "", _ec2_user_data, flags=re.MULTILINE)

    # Remove lines that start with '#' but not '#!'
    _ec2_user_data = re.sub(r"^(?!#!)#.*\n?", "", _ec2_user_data, flags=re.MULTILINE)

    # Finally remove blank lines
    _ec2_user_data = re.sub(r"^\s*\n", "", _ec2_user_data, flags=re.MULTILINE)

    return _ec2_user_data


class CustomResourceSendAnonymousMetrics(AWSCustomObject):
    resource_type = "Custom::SendAnonymousMetrics"
    props = {
        "ServiceToken": (str, True),
        "DesiredCapacity": (str, True),
        "InstanceType": (str, True),
        "Efa": (str, True),
        "ScratchSize": (str, True),
        "RootSize": (str, True),
        "SpotPrice": (str, True),
        "BaseOS": (str, True),
        "StackUUID": (str, True),
        "KeepForever": (str, True),
        "TerminateWhenIdle": (str, True),
        "FsxLustre": (str, True),
        "Dcv": (str, True),
        "Version": (str, True),
        "Region": (str, True),
        "Misc": (str, True),
    }


def is_bare_metal(instance_type: str) -> bool:
    return True if "metal" in instance_type.lower() else False


def is_cpu_options_supported(instance_type: str) -> bool:
    # CpuOptions is not supported for all instances
    # it's not explicitly called out in AWS docs, but this page does not list metal instances for CpuOptions:
    # https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/cpu-options-supported-instances-values.html

    # TODO - clean this up a bit , try/except
    _instance_details = ec2_client.describe_instance_types(
        InstanceTypes=[instance_type]
    ).get("InstanceTypes", {})[0]

    # If we are bare metal - no CpuOptions
    if _instance_details.get("BareMetal", False):
        return False

    _valid_threads_per_core: list = _instance_details.get("VCpuInfo", {}).get(
        "ValidThreadsPerCore", []
    )

    # Missing ValidThreadsPerCore means no CPUOptions support
    if not _valid_threads_per_core:
        return False

    # If our only entry (the last entry) is 1, we don't support CPU Options
    if _valid_threads_per_core[-1] == 1:
        return False

    # If we make it this far - it probably supports CpuOptions
    return True


def is_ebs_optimized(instance_type: str) -> bool:
    """
    Determine if a given instance_type supports EBS Optimization.
    """
    # TODO - try/except
    _instance_details = ec2_client.describe_instance_types(
        InstanceTypes=[instance_type]
    ).get("InstanceTypes", {})[0]

    _ebs_opt = (
        _instance_details.get("EbsInfo", {})
        .get("EbsOptimizedSupport", "unsupported")
        .lower()
    )

    if _ebs_opt in {"default", "supported"}:
        return True
    else:
        return False


def main(**params):
    try:
        # Metadata
        t = Template()
        t.set_version("2010-09-09")
        t.set_description(
            "(SOCA) - Base template to deploy compute nodes. Version 25.3.0"
        )

        _cluster_id: str = params.get("ClusterId", "unknown-cluster")

        debug = False
        mip_usage = False
        # list of instance type. Use + to specify more than one type
        # ex: c5.xlarge+c6.xlarge
        instances_list = params["InstanceType"]

        asg_lt = asg_LaunchTemplate()
        ltd = LaunchTemplateData("NodeLaunchTemplateData")
        mip = MixedInstancesPolicy()
        stack_name = Ref("AWS::StackName")

        # Begin LaunchTemplateData

        # Retrieve SOCA specific variable from AWS Parameter Store
        soca_parameters = SocaConfig(key="/").get_value(return_as=dict).get("message")
        if not soca_parameters:
            return {
                "success": False,
                "message": "Cloudformation_builder: Unable to query SSM for this SOCA environment.",
            }
        # Add SOCA job specific variables
        # job/xxx -> Job Specific (JobId, InstanceType, JobProject ...)
        # configuration/xxx -> SOCA environment specific (ClusterName, Base OS, Region ...)
        # system/xxx -> system related information (e.g: packages to install, DCV version, EFA version ...)
        for k, v in params.items():
            soca_parameters[f"/job/{k}"] = v

        # Create bootstrap UUID for this job
        _bootstrap_uuid = str(uuid.uuid4())

        # Location of Boostrap scripts on S3
        _bootstrap_s3_location_folder = f"{soca_parameters.get('/configuration/ClusterId')}/config/do_not_delete/bootstrap/compute_node/{_bootstrap_uuid}"

        # Add custom bootstrap path specific to current job id
        soca_parameters["/job/BootstrapPath"] = (
            f"/apps/soca/{soca_parameters.get('/configuration/ClusterId')}/shared/logs/bootstrap/compute_node/{soca_parameters.get('/job/JobId')}/{_bootstrap_uuid}"
        )

        # add custom NodeType
        soca_parameters["/job/NodeType"] = "compute_node"

        # Replace default SOCA wide BaseOs value with job specific OS
        soca_parameters["/configuration/BaseOS"] = params.get("BaseOS")

        soca_parameters["/job/BootstrapScriptsS3Location"] = (
            f"s3://{soca_parameters.get('/configuration/S3Bucket')}/{_bootstrap_s3_location_folder}/"
        )

        # Create User Data
        _render_user_data = SocaJinja2Generator(
            get_template=f"compute_node/01_user_data.sh.j2",
            template_dirs=[
                f"/opt/soca/{os.environ.get('SOCA_CLUSTER_ID')}/cluster_node_bootstrap/"
            ],
            variables=soca_parameters,
        ).to_stdout(autocast_values=True)

        if _render_user_data.get("success") is False:
            return SocaResponse(
                success=False,
                message=f"Unable to generate compute_node/01_user_data.sh.j2 Jinja2 template because of {_render_user_data.get('message')}",
            )
        else:
            _user_data = clean_user_data(
                text_to_remove=[
                    "# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
                    "# SPDX-License-Identifier: Apache-2.0",
                ],
                data=_render_user_data.get("message"),
            )

        # Create bootstrap setup invoked by user data
        # Create directory structure
        pathlib.Path(soca_parameters.get("/job/BootstrapPath")).mkdir(
            parents=True, exist_ok=True
        )

        # Bootstrap Sequence: Generate template and upload them to S3
        _templates_to_render = [
            "templates/linux/system_packages/install_required_packages",
            "templates/linux/filesystems_automount",
            "compute_node/02_setup",
            "compute_node/03_setup_post_reboot",
            "compute_node/04_setup_user_customization",
        ]

        for _t in _templates_to_render:
            # Render Template
            _render_bootstrap_setup_template = SocaJinja2Generator(
                get_template=f"{_t}.sh.j2",
                template_dirs=[
                    f"/opt/soca/{os.environ.get('SOCA_CLUSTER_ID')}/cluster_node_bootstrap/"
                ],
                variables=soca_parameters,
            ).to_s3(
                bucket_name=soca_parameters.get("/configuration/S3Bucket"),
                key=f"{_bootstrap_s3_location_folder}/{_t.split('/')[-1]}.sh",
                autocast_values=True,
            )

            if _render_bootstrap_setup_template.get("success") is False:
                return SocaResponse(
                    success=False,
                    message=f"Unable to generate {_t}.sh.j2 Jinja2 template because of {_render_bootstrap_setup_template.get('message')}",
                )

        # Specify the security groups to assign to the compute nodes. Max 5 per instance
        # TODO - the length vs. maxlength should be checked
        security_groups = [params["SecurityGroupId"]]
        if params["AdditionalSecurityGroupIds"]:
            for sg_id in params["AdditionalSecurityGroupIds"]:
                security_groups.append(sg_id)

        # Specify the IAM instance profile to use
        instance_profile = (
            params["ComputeNodeInstanceProfileArn"]
            if params["CustomIamInstanceProfile"] is False
            else params["CustomIamInstanceProfile"]
        )

        SpotFleet = (
            True
            if (
                (params["SpotPrice"] is not False)
                and (params["SpotAllocationCount"] is False)
                and (int(params["DesiredCapacity"]) > 1 or len(instances_list) > 1)
            )
            else False
        )
        ltd.EbsOptimized = True
        for instance in instances_list:
            ltd.EbsOptimized = is_ebs_optimized(instance_type=instance)

            if is_cpu_options_supported(instance_type=instance) and (
                SpotFleet is False or len(instances_list) == 1
            ):
                # Spotfleet with multiple instance types doesn't support CpuOptions
                # So we can't add CpuOptions if SpotPrice is specified and when multiple instances are specified
                ltd.CpuOptions = CpuOptions(
                    CoreCount=int(params["CoreCount"]),
                    ThreadsPerCore=1 if params["ThreadsPerCore"] is False else 2,
                )

        ltd.IamInstanceProfile = IamInstanceProfile(Arn=instance_profile)
        ltd.KeyName = params["SSHKeyPair"]
        ltd.ImageId = params["ImageId"]

        if params["SpotPrice"] is not False and params["SpotAllocationCount"] is False:
            if params["SpotPrice"] == "auto":
                # auto -> cap at OD price
                ltd.InstanceMarketOptions = InstanceMarketOptions(MarketType="spot")
            else:
                ltd.InstanceMarketOptions = InstanceMarketOptions(
                    MarketType="spot",
                    SpotOptions=SpotOptions(MaxPrice=str(params["SpotPrice"])),
                )
        ltd.InstanceType = instances_list[0]

        #
        # EFA Interface deployments
        #
        ltd.NetworkInterfaces = [
            NetworkInterfaces(
                InterfaceType=(
                    "efa" if params["Efa"] is not False else Ref("AWS::NoValue")
                ),
                DeleteOnTermination=True,
                DeviceIndex=0,
                Groups=security_groups,
            )
        ]
        if params.get("Efa", False) is not False:
            _max_efa_interfaces: int = params.get("MaxEfaInterfaces", 0)

            for _i in range(1, _max_efa_interfaces):
                ltd.NetworkInterfaces.append(
                    NetworkInterfaces(
                        InterfaceType="efa",
                        DeleteOnTermination=True,
                        DeviceIndex=1 if (_i > 0) else 0,
                        NetworkCardIndex=_i,
                        Groups=security_groups,
                    )
                )

        ltd.UserData = base64.b64encode(_user_data.encode("utf-8")).decode("utf-8")

        if params["BaseOS"] in {"amazonlinux2", "amazonlinux2023"}:
            _ebs_device_name = "/dev/xvda"
        else:
            _ebs_device_name = "/dev/sda1"
        _ebs_scratch_device_name = "/dev/xvdbx"

        # What is our default root volume_type?
        _volume_type: str = params.get("VolumeType", "gp2")

        ltd.BlockDeviceMappings = [
            LaunchTemplateBlockDeviceMapping(
                DeviceName=_ebs_device_name,
                Ebs=EBSBlockDevice(
                    VolumeSize=params["RootSize"],
                    VolumeType=_volume_type,
                    DeleteOnTermination=(
                        "false" if params["KeepEbs"] is True else "true"
                    ),
                    Encrypted=True,
                ),
            )
        ]

        if int(params["ScratchSize"]) > 0:
            ltd.BlockDeviceMappings.append(
                BlockDeviceMapping(
                    DeviceName=_ebs_scratch_device_name,
                    Ebs=EBSBlockDevice(
                        VolumeSize=params["ScratchSize"],
                        VolumeType=(
                            "io2" if int(params["VolumeTypeIops"]) > 0 else _volume_type
                        ),
                        Iops=(
                            params["VolumeTypeIops"]
                            if int(params["VolumeTypeIops"]) > 0
                            else Ref("AWS::NoValue")
                        ),
                        DeleteOnTermination=(
                            "false" if params["KeepEbs"] is True else "true"
                        ),
                        Encrypted=True,
                    ),
                )
            )
        ltd.TagSpecifications = [
            ec2.TagSpecifications(
                ResourceType="instance",
                Tags=base_Tags(
                    Name=str(params["ClusterId"])
                    + "-compute-job-"
                    + str(params["JobId"]),
                    _soca_JobId=str(params["JobId"]),
                    _soca_JobName=str(params["JobName"]),
                    _soca_JobQueue=str(params["JobQueue"]),
                    _soca_StackId=stack_name,
                    _soca_JobOwner=str(params["JobOwner"]),
                    _soca_JobProject=str(params["JobProject"]),
                    _soca_TerminateWhenIdle=str(params["TerminateWhenIdle"]),
                    _soca_KeepForever=str(params["KeepForever"]).lower(),
                    _soca_ClusterId=str(params["ClusterId"]),
                    _soca_NodeType="compute_node",
                ),
            )
        ]
        ltd.MetadataOptions = MetadataOptions(
            HttpEndpoint="enabled", HttpTokens=params["MetadataHttpTokens"]
        )

        if params["PlacementGroup"] is True:
            pg = PlacementGroup("ComputeNodePlacementGroup")
            pg.Strategy = "cluster"
            t.add_resource(pg)
            ltd.Placement = ec2.Placement(GroupName=Ref(pg))

        # End LaunchTemplateData

        # Begin Launch Template Resource
        lt = LaunchTemplate("NodeLaunchTemplate")
        lt.LaunchTemplateName = params["ClusterId"] + "-" + str(params["JobId"])
        lt.LaunchTemplateData = ltd

        t.add_resource(lt)
        # End Launch Template Resource

        if SpotFleet is True:
            # SpotPrice is defined and DesiredCapacity > 1 or need to try more than 1 instance_type
            # Create SpotFleet

            # Begin SpotFleetRequestConfigData Resource
            sfrcd = ec2.SpotFleetRequestConfigData()
            sfrcd.AllocationStrategy = params["SpotAllocationStrategy"]
            sfrcd.ExcessCapacityTerminationPolicy = "noTermination"
            sfrcd.IamFleetRole = params["SpotFleetIAMRoleArn"]
            sfrcd.InstanceInterruptionBehavior = "terminate"
            if params["SpotPrice"] != "auto":
                sfrcd.SpotPrice = str(params["SpotPrice"])
            sfrcd.SpotMaintenanceStrategies = ec2.SpotMaintenanceStrategies(
                CapacityRebalance=ec2.SpotCapacityRebalance(
                    ReplacementStrategy="launch"
                )
            )
            sfrcd.TargetCapacity = params["DesiredCapacity"]
            sfrcd.Type = "maintain"
            sfltc = ec2.LaunchTemplateConfigs()
            sflts = ec2.FleetLaunchTemplateSpecification(
                LaunchTemplateId=Ref(lt), Version=GetAtt(lt, "LatestVersionNumber")
            )
            sfltc.LaunchTemplateSpecification = sflts
            sfltc.Overrides = []
            for subnet in params["SubnetId"]:
                for index, instance in enumerate(instances_list):
                    if params["WeightedCapacity"] is not False:
                        sfltc.Overrides.append(
                            ec2.LaunchTemplateOverrides(
                                InstanceType=instance,
                                SubnetId=subnet,
                                WeightedCapacity=params["WeightedCapacity"][index],
                            )
                        )
                    else:
                        sfltc.Overrides.append(
                            ec2.LaunchTemplateOverrides(
                                InstanceType=instance, SubnetId=subnet
                            )
                        )
            sfrcd.LaunchTemplateConfigs = [sfltc]
            TagSpecifications = ec2.SpotFleetTagSpecification(
                ResourceType="spot-fleet-request",
                Tags=base_Tags(
                    Name=str(params["ClusterId"])
                    + "-compute-job-"
                    + str(params["JobId"]),
                    _soca_JobId=str(params["JobId"]),
                    _soca_JobName=str(params["JobName"]),
                    _soca_JobQueue=str(params["JobQueue"]),
                    _soca_StackId=stack_name,
                    _soca_JobOwner=str(params["JobOwner"]),
                    _soca_JobProject=str(params["JobProject"]),
                    _soca_TerminateWhenIdle=str(params["TerminateWhenIdle"]),
                    _soca_KeepForever=str(params["KeepForever"]).lower(),
                    _soca_ClusterId=str(params["ClusterId"]),
                    _soca_NodeType="compute_node",
                ),
            )
            # End SpotFleetRequestConfigData Resource

            # Begin SpotFleet Resource
            spotfleet = ec2.SpotFleet("SpotFleet")
            spotfleet.SpotFleetRequestConfigData = sfrcd
            t.add_resource(spotfleet)
            # End SpotFleet Resource
        else:
            asg_lt.LaunchTemplateSpecification = LaunchTemplateSpecification(
                LaunchTemplateId=Ref(lt), Version=GetAtt(lt, "LatestVersionNumber")
            )

            asg_lt.Overrides = []
            for index, instance in enumerate(instances_list):
                if params["WeightedCapacity"] is not False:
                    mip_usage = True
                    asg_lt.Overrides.append(
                        LaunchTemplateOverrides(
                            InstanceType=instance,
                            WeightedCapacity=str(params["WeightedCapacity"][index]),
                        )
                    )
                else:
                    asg_lt.Overrides.append(
                        LaunchTemplateOverrides(InstanceType=instance)
                    )

            # Begin InstancesDistribution
            if (
                params["SpotPrice"] is not False
                and params["SpotAllocationCount"] is not False
                and (
                    int(params["DesiredCapacity"]) - int(params["SpotAllocationCount"])
                )
                > 0
            ):
                mip_usage = True
                idistribution = InstancesDistribution()
                idistribution.OnDemandAllocationStrategy = (
                    "prioritized"  # only supported value
                )
                idistribution.OnDemandBaseCapacity = (
                    params["DesiredCapacity"] - params["SpotAllocationCount"]
                )
                idistribution.OnDemandPercentageAboveBaseCapacity = (
                    "0"  # force the other instances to be SPOT
                )
                idistribution.SpotMaxPrice = (
                    Ref("AWS::NoValue")
                    if params["SpotPrice"] == "auto"
                    else str(params["SpotPrice"])
                )
                idistribution.SpotAllocationStrategy = params["SpotAllocationStrategy"]
                mip.InstancesDistribution = idistribution

            # End MixedPolicyInstance

            # HPCJobDeploymentMethod selection

            # _soca_cluster_configuration: dict = get_soca_configuration(clusterid=)

            # Begin AutoScalingGroup Resource
            asg = AutoScalingGroup("AutoScalingComputeGroup")
            asg.DependsOn = "NodeLaunchTemplate"
            if mip_usage is True or len(instances_list) > 1:
                mip.LaunchTemplate = asg_lt
                asg.MixedInstancesPolicy = mip

            else:
                asg.LaunchTemplate = LaunchTemplateSpecification(
                    LaunchTemplateId=Ref(lt), Version=GetAtt(lt, "LatestVersionNumber")
                )

            asg.MinSize = int(params["DesiredCapacity"])
            asg.MaxSize = int(params["DesiredCapacity"])
            asg.VPCZoneIdentifier = params["SubnetId"]
            asg.CapacityRebalance = False
            asg.Tags = Tags(
                Name=str(params["ClusterId"]) + "-compute-job-" + str(params["JobId"]),
                _soca_JobId=str(params["JobId"]),
                _soca_JobName=str(params["JobName"]),
                _soca_JobQueue=str(params["JobQueue"]),
                _soca_StackId=stack_name,
                _soca_JobOwner=str(params["JobOwner"]),
                _soca_JobProject=str(params["JobProject"]),
                _soca_TerminateWhenIdle=str(params["TerminateWhenIdle"]),
                _soca_KeepForever=str(params["KeepForever"]).lower(),
                _soca_ClusterId=str(params["ClusterId"]),
                _soca_NodeType="compute_node",
            )

            # t.add_resource(asg)

            # HPC Fleet

            _fleet_overrides: list = []
            for _subnet in params["SubnetId"]:
                for _index, _instance in enumerate(instances_list):
                    if params["WeightedCapacity"] is not False:
                        _fleet_overrides.append(
                            ec2.FleetLaunchTemplateOverridesRequest(
                                SubnetId=_subnet,
                                InstanceType=_instance,
                                WeightedCapacity=str(
                                    params["WeightedCapacity"][_index]
                                ),
                            )
                        )
                    else:
                        _fleet_overrides.append(
                            ec2.FleetLaunchTemplateOverridesRequest(
                                SubnetId=_subnet,
                                InstanceType=_instance,
                            )
                        )

            # XXX FIXME TODO
            # Need to make sure the instance type is available in the AZ/subnet
            # As the Override generation with incompatible deployment would cause the entire API
            # to reject even if it could be fulfilled by another AZ.
            # This resolution takes place with the EC2 API for describe-offerings
            #

            _ec2_fleet = ec2.EC2Fleet(title="Ec2Fleet", Type="instant")
            _ec2_fleet.LaunchTemplateConfigs = [
                ec2.FleetLaunchTemplateConfigRequest(
                    LaunchTemplateSpecification=ec2.FleetLaunchTemplateSpecificationRequest(
                        LaunchTemplateId=Ref(lt),
                        Version=GetAtt(lt, "LatestVersionNumber"),
                    ),
                    Overrides=_fleet_overrides,
                )
            ]

            # Spot support for EC2 Fleet
            if (
                params["SpotPrice"] is not False
                and params["SpotAllocationCount"] is False
            ):
                _spot_options_request = ec2.SpotOptionsRequest()
                _spot_options_request.InstanceInterruptionBehavior = "terminate"
                _spot_options_request.AllocationStrategy = params[
                    "SpotAllocationStrategy"
                ]
                _ec2_fleet.SpotOptions = _spot_options_request
                _ec2_fleet.TargetCapacitySpecification = (
                    ec2.TargetCapacitySpecificationRequest(
                        TotalTargetCapacity=int(params["DesiredCapacity"]),
                        DefaultTargetCapacityType="spot",
                    )
                )

            else:
                _ec2_fleet.TargetCapacitySpecification = (
                    ec2.TargetCapacitySpecificationRequest(
                        TotalTargetCapacity=int(params["DesiredCapacity"]),
                        DefaultTargetCapacityType="on-demand",
                        OnDemandTargetCapacity=int(params["DesiredCapacity"]),
                    )
                )

            t.add_resource(_ec2_fleet)

            # End AutoScalingGroup Resource

        # Begin FSx for Lustre
        if params["FSxLustreConfiguration"]["fsx_lustre"] is not False:
            if params["FSxLustreConfiguration"]["existing_fsx"] is False:
                fsx_lustre = FileSystem("FSxForLustre")
                fsx_lustre.FileSystemType = "LUSTRE"
                fsx_lustre.FileSystemTypeVersion = "2.15"
                fsx_lustre.StorageCapacity = params["FSxLustreConfiguration"][
                    "capacity"
                ]
                fsx_lustre.SecurityGroupIds = security_groups
                fsx_lustre.SubnetIds = params["SubnetId"]
                fsx_lustre_configuration = LustreConfiguration()
                fsx_lustre_configuration.DeploymentType = params[
                    "FSxLustreConfiguration"
                ]["deployment_type"].upper()
                if params["FSxLustreConfiguration"]["deployment_type"].upper() in {
                    "PERSISTENT_1",
                    "PERSISTENT_2",
                }:
                    fsx_lustre_configuration.PerUnitStorageThroughput = params[
                        "FSxLustreConfiguration"
                    ]["per_unit_throughput"]

                if params["FSxLustreConfiguration"]["s3_backend"] is not False:
                    fsx_lustre_configuration.ImportPath = (
                        params["FSxLustreConfiguration"]["import_path"]
                        if params["FSxLustreConfiguration"]["import_path"] is not False
                        else params["FSxLustreConfiguration"]["s3_backend"]
                    )
                    fsx_lustre_configuration.ExportPath = (
                        params["FSxLustreConfiguration"]["import_path"]
                        if params["FSxLustreConfiguration"]["import_path"] is not False
                        else params["FSxLustreConfiguration"]["s3_backend"]
                        + "/"
                        + params["ClusterId"]
                        + "-fsxoutput/job-"
                        + params["JobId"]
                        + "/"
                    )

                fsx_lustre.LustreConfiguration = fsx_lustre_configuration
                fsx_lustre.Tags = base_Tags(
                    # False disable PropagateAtLaunch
                    Name=str(params["ClusterId"] + "-compute-job-" + params["JobId"]),
                    _soca_JobId=str(params["JobId"]),
                    _soca_JobName=str(params["JobName"]),
                    _soca_JobQueue=str(params["JobQueue"]),
                    _soca_TerminateWhenIdle=str(params["TerminateWhenIdle"]),
                    _soca_StackId=stack_name,
                    _soca_JobOwner=str(params["JobOwner"]),
                    _soca_JobProject=str(params["JobProject"]),
                    _soca_KeepForever=str(params["KeepForever"]).lower(),
                    _soca_NodeType="compute_node",
                    _soca_FSx="true",
                    _soca_ClusterId=str(params["ClusterId"]),
                )
                t.add_resource(fsx_lustre)
        # End FSx For Lustre

        # Begin Custom Resource
        # Change Mapping to No if you want to disable this
        if params.get("MetricCollectionAnonymous", False) is True:
            metrics = CustomResourceSendAnonymousMetrics("SendAnonymousData")
            metrics.ServiceToken = params["SolutionMetricsLambda"]
            metrics.DesiredCapacity = str(params["DesiredCapacity"])
            metrics.InstanceType = str(params["InstanceType"])
            metrics.Efa = str(params["Efa"])
            metrics.ScratchSize = str(params["ScratchSize"])
            metrics.RootSize = str(params["RootSize"])
            metrics.SpotPrice = str(params["SpotPrice"])
            metrics.BaseOS = str(params["BaseOS"])
            metrics.StackUUID = str(params["StackUUID"])
            metrics.KeepForever = str(params["KeepForever"])
            # remove potentially sensitive information
            fsx_l_metric = {}
            fsx_l_config = params.get("FSxLustreConfiguration", {})
            if fsx_l_config.get("fsx_lustre", False):
                fsx_l_metric["fsx_lustre"] = True
                fsx_l_metric["deployment_type"] = fsx_l_config.get(
                    "deployment_type", "SCRATCH_2"
                ).upper()
                fsx_l_metric["capacity"] = fsx_l_config.get("capacity", 1200)
                fsx_l_metric["per_unit_throughput"] = fsx_l_config.get(
                    "per_unit_throughput", 200
                )
                # Replace these with simple True/False - not the actual user values
                for item in {
                    "existing_fsx",
                    "s3_backend",
                    "import_path",
                    "export_path",
                }:
                    fsx_l_metric[item] = True if fsx_l_config.get(item) else False
            else:
                fsx_l_metric["fsx_lustre"] = False
            metrics.FsxLustre = str(fsx_l_metric)
            metrics.TerminateWhenIdle = str(params["TerminateWhenIdle"])
            metrics.Dcv = "false"
            metrics.Region = params.get("Region", "")
            metrics.Version = params.get("Version", "")
            metrics.Misc = params.get("Misc", "")
            t.add_resource(metrics)
        # End Custom Resource

        if debug is True:
            print(t.to_json())

        # Tags must use "soca:<Key>" syntax
        template_output = t.to_yaml().replace("_soca_", "soca:")
        return {"success": True, "output": template_output}

    except Exception as e:
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        return {
            "success": False,
            "output": "cloudformation_builder.py: "
            + (
                str(e)
                + ": error :"
                + str(exc_type)
                + " "
                + str(fname)
                + " "
                + str(exc_tb.tb_lineno)
            ),
        }
