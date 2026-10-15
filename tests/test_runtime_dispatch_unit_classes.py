"""2026-10-16 stage: runtime_activation_unavailable with no hint; the cause (psycopg InsufficientPrivilege) was only in the PG log.
runtime_dispatch.exception_classes names the class chain, never a message or value."""
from nexloop_eios.runtime_dispatch import exception_classes


class Hidden(Exception):
    pass


def raise_hidden():
    try:
        raise PermissionError('secret detail /path value=42')
    except PermissionError:
        raise Hidden('runtime activation unavailable') from None


def test_the_class_chain_is_named_even_when_the_cause_is_hidden():
    try:
        raise_hidden()
    except Hidden as error:
        names = exception_classes(error)
    assert names == ['Hidden', 'PermissionError']
    assert all('secret' not in n and '42' not in n for n in names)


def test_an_explicit_cause_and_a_single_error():
    try:
        try:
            raise KeyError('x')
        except KeyError as inner:
            raise ValueError('y') from inner
    except ValueError as error:
        assert exception_classes(error) == ['ValueError', 'KeyError']
    assert exception_classes(RuntimeError('z')) == ['RuntimeError']
