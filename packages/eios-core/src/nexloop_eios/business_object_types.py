"""Versioned NexLoop core business object types (deploy/ontology/business-object-types.v*.json).

NX-026 D1: Commitment (M18) is a NexLoop core type published at deployment through trusted
configuration (0050), like the system metadata types, but kept in its own declaration: business
types are written by service principals through governed Actions and derived per-object
authority, which the system metadata declaration forbids. Same shape and validation otherwise.
"""
from nexloop_eios import system_object_types

SCHEMA='nexloop-business-object-types/1'
BusinessObjectTypesRejected=system_object_types.SystemObjectTypesRejected


def validate(value):return system_object_types.validate(value,schema=SCHEMA)


def load(path):return system_object_types.load(path,schema=SCHEMA)


def trusted_object_types(path):
    """Rows for the trusted-configuration manifest ``object_types`` list."""
    return system_object_types.trusted_object_types(path,schema=SCHEMA)
