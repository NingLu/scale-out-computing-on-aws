# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import click
from commands.common import print_output, is_controller_instance
from utils.cache import SocaCacheClient
import sys


def cache_client(is_admin: bool = False) -> SocaCacheClient:
    if is_admin:
        if is_controller_instance():
            return SocaCacheClient(is_admin=True)
        else:
            print_output(
                f"This command can only be executed from the SOCA controller host",
                error=True,
            )
    else:
        return SocaCacheClient(is_admin=False)


def check_protected_keys(key: str):
    _protected_str = ["/configuration", "/system", "/packages"]
    for path in _protected_str:
        if path in key:
            print_output(
                "Cannot modify protected keys. To update the cache for this key, modify 'config' first.",
                error=True,
            )
    else:
        return False


@click.group()
def cache():
    pass


@cache.command()
@click.option(
    "-k",
    "--key",
    required=True,
    help="Specify a SOCA configuration key or configuration tree",
)
@click.option(
    "--output",
    default="text",
    type=click.Choice(["text", "json", "yaml"]),
    help="Output result as: text, json, yaml",
)
def get(key, output):
    _q = cache_client().get(key=key).get("message")
    if _q == "CACHE_MISS":
        print_output("Key not found in cache", error=True)
    else:
        print_output(_q.decode(), output=output)


@cache.command()
@click.option(
    "-k",
    "--key",
    required=True,
    help="Specify a SOCA configuration key or configuration tree",
)
@click.option(
    "-v",
    "--value",
    required=True,
    help="Update Cache key value",
)
@click.option(
    "-e",
    "--expire",
    type=int,
    default=86400,
    help="Optional: Set Expiration",
)
def set(key, value, expire, called_from_config=False):
    if called_from_config is True:
        _q = cache_client(is_admin=True).set(key=key, value=value, ex=expire)
    else:
        if check_protected_keys(key) is False:
            _q = cache_client(is_admin=True).set(key=key, value=value, ex=expire)

    print_output("Cache updated")


@cache.command()
@click.option(
    "-k",
    "--key",
    required=True,
    help="Specify a SOCA configuration key or configuration tree",
)
def delete(key):
    _q = cache_client(is_admin=True).delete(key=key)
    print_output("Key deleted from cache")


@cache.command()
@click.option(
    "-p",
    "--pattern",
    required=True,
    help="Filter specific keys, for example: soca*",
)
def scan(pattern):
    _q = cache_client(is_admin=True).scan(match_pattern=pattern)
    print_output(_q.get("message"))
