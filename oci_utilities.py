from pathlib import Path

import oci


def load_config(config_file="~/.oci/config", profile="DEFAULT"):
    return oci.config.from_file(str(Path(config_file).expanduser()), profile)


def get_identity_client(config):
    return oci.identity.IdentityClient(config)


def get_compute_client(config):
    return oci.core.ComputeClient(config)


def get_tenancy_ocid(config, tenancy_ocid=None):
    return tenancy_ocid or config["tenancy"]


def get_compartment_lookup(identity_client, tenancy_ocid):
    tenancy = identity_client.get_tenancy(tenancy_ocid).data
    compartments = oci.pagination.list_call_get_all_results(
        identity_client.list_compartments,
        tenancy_ocid,
        compartment_id_in_subtree=True,
        access_level="ANY",
    ).data

    lookup = {
        tenancy.id: {
            "id": tenancy.id,
            "name": tenancy.name,
            "parent_id": None,
        }
    }

    for compartment in compartments:
        lookup[compartment.id] = {
            "id": compartment.id,
            "name": compartment.name,
            "parent_id": compartment.compartment_id,
        }

    return lookup


def get_descendant_compartment_ids(lookup, root_compartment_id, include_root=False):
    descendant_ids = []
    pending_parent_ids = [root_compartment_id]

    while pending_parent_ids:
        parent_id = pending_parent_ids.pop()
        for compartment in lookup.values():
            if compartment["parent_id"] == parent_id:
                descendant_ids.append(compartment["id"])
                pending_parent_ids.append(compartment["id"])

    if include_root:
        return [root_compartment_id, *descendant_ids]
    return descendant_ids


def find_compartment_id(identity_client, tenancy_ocid, compartment_name_or_id=None):
    if not compartment_name_or_id:
        return tenancy_ocid

    lookup = get_compartment_lookup(identity_client, tenancy_ocid)
    if compartment_name_or_id in lookup:
        return compartment_name_or_id

    matches = [
        compartment["id"]
        for compartment in lookup.values()
        if compartment["name"] == compartment_name_or_id
    ]
    if not matches:
        raise ValueError(
            f"Compartment '{compartment_name_or_id}' was not found in the tenancy subtree."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Compartment name '{compartment_name_or_id}' is ambiguous; use the OCID instead."
        )
    return matches[0]
