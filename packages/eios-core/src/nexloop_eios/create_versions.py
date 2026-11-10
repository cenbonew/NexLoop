"""The create Action version of a new object (P0 2026-11-10, L6 Q6).

A new object is written at its type's latest published version, by the active `<Type>.create` that covers exactly that version and
that the calling service holds a live execute grant on (authz.nexloop_create_action_version). When either is missing the database
raises and so does this module: never a silent fall back to version 1, which after a Schema publication wrote new objects one version
behind the existing ones (stage 11-10, customer-5) or an object no governed Action could edit."""


class CreateVersionUnavailable(PermissionError):
    pass


def create_action_version(service, type_name):
    """The `<type_name>.create` version for this service's next new object (a backend service client: create_object's owner)."""
    backend, session = service._backend, service._session
    try:
        backend._assert_open()
        with backend._pool.connection() as db:
            version = db.execute('select authz.nexloop_create_action_version(%s,%s,%s)', (session.token_digest, session.world, type_name)).fetchone()[0]
    except Exception:
        raise CreateVersionUnavailable('create_version_unavailable') from None
    if type(version) is not int or version < 1:
        raise CreateVersionUnavailable('create_version_unavailable')
    return version


def create_current(service, *, intent_id, type_name, properties):
    """create_object at the type's latest published version (see the module docstring)."""
    return service.create_object(action_name=type_name + '.create', action_version=create_action_version(service, type_name),
                                 intent_id=intent_id, type_name=type_name, properties=properties)
