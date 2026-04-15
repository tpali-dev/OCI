import argparse
import sys
import time

import oci

from oci_utilities import (
    find_compartment_id,
    get_compartment_lookup,
    get_compute_client,
    get_descendant_compartment_ids,
    get_identity_client,
    get_tenancy_ocid,
    load_config,
)


LIFECYCLE_ACTIONS = {
    "start": "START",
    "stop": "STOP",
}

WAIT_TARGET_STATES = {
    "start": oci.core.models.Instance.LIFECYCLE_STATE_RUNNING,
    "stop": oci.core.models.Instance.LIFECYCLE_STATE_STOPPED,
}

FAILED_WAIT_STATES = {
    "start": {oci.core.models.Instance.LIFECYCLE_STATE_STOPPED},
    "stop": {oci.core.models.Instance.LIFECYCLE_STATE_RUNNING},
}

DEFAULT_WAIT_TIMEOUT_SECONDS = 120


def truncate_value(value, max_width):
    if len(value) <= max_width:
        return value
    return f"{value[: max_width - 3]}..."


def truncate_middle(value, max_width):
    if len(value) <= max_width:
        return value
    if max_width <= 3:
        return value[:max_width]

    visible_chars = max_width - 3
    left_chars = visible_chars // 2
    right_chars = visible_chars - left_chars
    return f"{value[:left_chars]}...{value[-right_chars:]}"


def format_table(rows, headers):
    string_rows = [[str(value) for value in row] for row in rows]
    widths = []
    for index, header in enumerate(headers):
        column_values = [row[index] for row in string_rows]
        widths.append(max(len(header), *(len(value) for value in column_values)))

    header_line = "  ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    )
    separator_line = "  ".join("-" * width for width in widths)
    data_lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in string_rows
    ]
    return "\n".join([header_line, separator_line, *data_lines])


def build_context(config_file="~/.oci/config", profile="DEFAULT", tenancy_ocid=None):
    config = load_config(config_file=config_file, profile=profile)
    resolved_tenancy_ocid = get_tenancy_ocid(config, tenancy_ocid)
    return {
        "config": config,
        "tenancy_ocid": resolved_tenancy_ocid,
        "identity_client": get_identity_client(config),
        "compute_client": get_compute_client(config),
    }


def resolve_compartment_id(context, compartment_name_or_id=None):
    return find_compartment_id(
        context["identity_client"],
        context["tenancy_ocid"],
        compartment_name_or_id,
    )


def list_instances(context, compartment_name_or_id=None, include_subtree=False):
    compartment_id = resolve_compartment_id(context, compartment_name_or_id)
    compartment_ids = [compartment_id]

    if include_subtree:
        lookup = get_compartment_lookup(
            context["identity_client"],
            context["tenancy_ocid"],
        )
        compartment_ids = get_descendant_compartment_ids(
            lookup,
            compartment_id,
            include_root=True,
        )

    instances = []
    for current_compartment_id in compartment_ids:
        instances.extend(
            oci.pagination.list_call_get_all_results(
                context["compute_client"].list_instances,
                compartment_id=current_compartment_id,
            ).data
        )

    return instances


def find_instance(context, compartment_name_or_id, instance_name_or_id):
    compartment_id = resolve_compartment_id(context, compartment_name_or_id)
    instances = list_instances(context, compartment_id)

    for instance in instances:
        if instance.id == instance_name_or_id:
            return instance

    matches = [
        instance for instance in instances if instance.display_name == instance_name_or_id
    ]
    if not matches:
        raise ValueError(
            f"Instance '{instance_name_or_id}' was not found in compartment '{compartment_name_or_id or context['tenancy_ocid']}'."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Instance name '{instance_name_or_id}' is ambiguous in the selected compartment; use the OCID instead."
        )
    return matches[0]


def print_instances(instances, verbose=False):
    if not instances:
        print("No instances found.")
        return

    rows = []
    for instance in sorted(instances, key=lambda item: item.display_name.lower()):
        compartment_id = instance.compartment_id
        instance_id = instance.id
        if not verbose:
            compartment_id = truncate_middle(compartment_id, 38)
            instance_id = truncate_middle(instance_id, 38)

        rows.append(
            [
                truncate_value(instance.display_name, 24),
                instance.lifecycle_state,
                truncate_value(instance.shape, 24),
                instance.availability_domain,
                compartment_id,
                instance_id,
            ]
        )

    print(
        format_table(
            rows,
            headers=[
                "Name",
                "State",
                "Shape",
                "Availability Domain",
                "Compartment OCID",
                "Instance OCID",
            ],
        )
    )


def wait_for_instance_state(
    compute_client,
    instance_id,
    target_state,
    failed_states=None,
    timeout_seconds=DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds=5,
):
    start_time = time.time()
    last_state = None
    failed_states = failed_states or set()

    while True:
        instance = compute_client.get_instance(instance_id).data
        current_state = instance.lifecycle_state

        if current_state != last_state:
            print(f"Current lifecycle state: {current_state}")
            last_state = current_state

        if current_state == target_state:
            return instance

        if current_state in failed_states:
            raise RuntimeError(
                f"Instance reached unexpected lifecycle state {current_state} "
                f"before reaching {target_state}."
            )

        elapsed_seconds = time.time() - start_time
        if elapsed_seconds >= timeout_seconds:
            raise TimeoutError(
                f"Timed out after {timeout_seconds} seconds waiting for instance "
                f"to reach {target_state}. Last known state: {current_state}."
            )

        time.sleep(poll_interval_seconds)


def change_instance_state(
    context,
    action,
    instance_name_or_id,
    compartment_name_or_id=None,
    wait=False,
    wait_timeout_seconds=DEFAULT_WAIT_TIMEOUT_SECONDS,
):
    instance = find_instance(context, compartment_name_or_id, instance_name_or_id)
    action_name = LIFECYCLE_ACTIONS[action]

    response = context["compute_client"].instance_action(instance.id, action_name)
    print(
        f"{action.capitalize()} requested for '{instance.display_name}' ({instance.id})."
    )

    if wait:
        target_state = WAIT_TARGET_STATES[action]
        print(f"Waiting for instance to reach {target_state}...")
        try:
            wait_for_instance_state(
                context["compute_client"],
                instance.id,
                target_state,
                failed_states=FAILED_WAIT_STATES.get(action),
                timeout_seconds=wait_timeout_seconds,
            )
        except RuntimeError as exc:
            if action == "start":
                raise RuntimeError(
                    f"Instance '{instance.display_name}' failed to reach {target_state} "
                    "and returned to STOPPED after the startup attempt."
                ) from exc
            raise
        print(f"Instance is now {target_state}.")

    return response


def main():
    parser = argparse.ArgumentParser(
        description=(
            "OCI helper tool for listing and managing compute instances.\n\n"
            "Subcommands:\n"
            "  list   List instances in a compartment; use --include-subtree to search descendants.\n"
            "  start  Start an instance by OCID or exact display name.\n"
            "  stop   Stop an instance by OCID or exact display name."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config-file",
        default="~/.oci/config",
        help="Path to OCI config file",
    )
    parser.add_argument(
        "--profile",
        default="DEFAULT",
        help="OCI config profile name",
    )
    parser.add_argument(
        "--tenancy-ocid",
        help="OCI tenancy OCID. Defaults to the tenancy in the selected OCI profile.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list",
        help="List compute instances",
        description=(
            "List compute instances in a compartment.\n\n"
            "If --compartment is omitted, the tenancy root compartment is used.\n"
            "\n"
            "Use --include-subtree to include descendant compartments in the search.\n"
            "Use --verbose to print full OCIDs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    list_parser.add_argument(
        "--compartment",
        help="Compartment name or OCID. Defaults to the root tenancy compartment.",
    )
    list_parser.add_argument(
        "--include-subtree",
        action="store_true",
        help="Also list instances from descendant compartments.",
    )
    list_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full OCIDs without truncation.",
    )

    for command in ("start", "stop"):
        action_parser = subparsers.add_parser(
            command,
            help=f"{command.capitalize()} an instance",
            description=(
                f"{command.capitalize()} an instance by OCID or exact display name.\n\n"
                "If --compartment is omitted, the tenancy root compartment is used for name resolution.\n"
                "\n"
                "Use --compartment when resolving an instance by name.\n"
                "Use --wait to block until the target lifecycle state is reached.\n"
                "Use --wait-timeout-seconds to control how long to wait."
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        action_parser.add_argument(
            "instance",
            help="Instance OCID or exact display name",
        )
        action_parser.add_argument(
            "--compartment",
            help="Compartment name or OCID used to resolve the instance name",
        )
        action_parser.add_argument(
            "--wait",
            action="store_true",
            help="Wait until the instance reaches the target lifecycle state",
        )
        action_parser.add_argument(
            "--wait-timeout-seconds",
            type=int,
            default=DEFAULT_WAIT_TIMEOUT_SECONDS,
            help=(
                "Maximum seconds to wait when --wait is used. "
                f"Defaults to {DEFAULT_WAIT_TIMEOUT_SECONDS}."
            ),
        )

    args = parser.parse_args()
    try:
        context = build_context(
            config_file=args.config_file,
            profile=args.profile,
            tenancy_ocid=args.tenancy_ocid,
        )

        if args.command == "list":
            print_instances(
                list_instances(
                    context,
                    compartment_name_or_id=args.compartment,
                    include_subtree=args.include_subtree,
                ),
                verbose=args.verbose,
            )
            return

        change_instance_state(
            context,
            action=args.command,
            instance_name_or_id=args.instance,
            compartment_name_or_id=args.compartment,
            wait=args.wait,
            wait_timeout_seconds=args.wait_timeout_seconds,
        )
    except (
        RuntimeError,
        TimeoutError,
        ValueError,
        oci.exceptions.ServiceError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
