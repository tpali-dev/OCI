import argparse

from anytree import Node, RenderTree

from oci_utilities import (
    get_compartment_lookup,
    get_identity_client,
    get_tenancy_ocid,
    load_config,
)


def print_compartment_tree(tenancy_ocid=None, config_file="~/.oci/config", profile="DEFAULT"):
    config = load_config(config_file=config_file, profile=profile)
    tenancy_ocid = get_tenancy_ocid(config, tenancy_ocid)
    identity_client = get_identity_client(config)
    lookup = get_compartment_lookup(identity_client, tenancy_ocid)

    nodes = {}

    # Create nodes
    for compartment in lookup.values():
        nodes[compartment["id"]] = Node(compartment["name"])

    # Assign parents
    for compartment in lookup.values():
        parent_id = compartment["parent_id"]
        if parent_id in nodes:
            nodes[compartment["id"]].parent = nodes[parent_id]

    # Find root(s)
    roots = [n for n in nodes.values() if n.is_root]

    # Print all trees
    for root in roots:
        for pre, _, node in RenderTree(root):
            print(f"{pre}{node.name}")


def main():
    parser = argparse.ArgumentParser(
        description="Print an OCI compartment tree for a tenancy."
    )
    parser.add_argument(
        "tenancy_ocid",
        nargs="?",
        help="OCI tenancy OCID. Defaults to the tenancy in the selected OCI profile.",
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
    args = parser.parse_args()

    print_compartment_tree(
        tenancy_ocid=args.tenancy_ocid,
        config_file=args.config_file,
        profile=args.profile,
    )


if __name__ == "__main__":
    main()
