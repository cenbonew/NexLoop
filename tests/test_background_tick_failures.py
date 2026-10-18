"""Background services (2026-10-18 stage: the alert evaluator's state stopped at 2026-10-17 14:33Z with nothing in its log): a
failed tick is one structured line (exception class, SQLSTATE, count; never a message), repeated every 30th consecutive failure,
and the recovery is logged; the loop keeps running as before."""
import io
import json
import logging

import psycopg

from nexloop_eios.background_services import TickFailures
from nexloop_eios.structured_log import StructuredFormatter


def capture():
    stream = io.StringIO()
    logger = logging.getLogger('nexloop.background.test')
    logger.handlers[:] = []
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter('alert-evaluator'))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    return logger, stream


def lines(stream):
    return [json.loads(x) for x in stream.getvalue().splitlines()]


def test_first_every_thirtieth_and_recovery_are_logged_with_codes_only():
    logger, stream = capture()
    failures = TickFailures('alert-evaluator', logger=logger)
    secret = 'password=synthetic-secret host=10.0.0.1'
    for _ in range(61):
        failures.failed(PermissionError(secret))
    failures.succeeded()
    failures.succeeded()  # nothing more: no failure since
    out = lines(stream)
    assert [(x['event'], x['detail']['count']) for x in out] == [
        ('background tick failed', 1), ('background tick failed', 30), ('background tick failed', 60), ('background tick recovered', 61)]
    assert out[0]['detail'] == {'code': 'PermissionError', 'stage': 'tick', 'count': 1} and out[0]['service'] == 'alert-evaluator'
    assert 'synthetic-secret' not in stream.getvalue() and '10.0.0.1' not in stream.getvalue()
    assert failures.count == 0


def test_the_sqlstate_of_a_database_failure_is_kept():
    logger, stream = capture()
    failures = TickFailures('alert-evaluator', logger=logger)
    try:
        try:
            raise psycopg.errors.InsufficientPrivilege('permission denied for function nexloop_alert_evaluate')
        except psycopg.Error:
            raise PermissionError('denied') from None
    except PermissionError as error:
        failures.failed(error)
    [line] = lines(stream)
    assert line['detail'] == {'code': 'InsufficientPrivilege', 'sqlstate': '42501', 'stage': 'tick', 'count': 1}
    assert 'nexloop_alert_evaluate' not in stream.getvalue()
