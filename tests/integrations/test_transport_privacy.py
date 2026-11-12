"""Provider credentials must not escape through the existing telemetry sinks."""

import json
import logging
import sys
from collections.abc import Callable, Mapping
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sentry_sdk

from apps.delivery.privacy import delivery_secrets
from apps.integrations.privacy import MARKER, scrub_provider_text, scrub_text
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import ProviderRequest, TransportFailure
from config.settings import prod
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply, permitted_send
from tests.integrations.test_transport import KEY
from tests.integrations.transport_tls import HOST, route_storage_host, tls_context
from tests.security.test_credential_log_hygiene import _CaptureTransport

if TYPE_CHECKING:
    from sentry_sdk.types import Event


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "jwt_token",
        "autenticar_procurador_token",
        "X-Access-Token",
        "X-Broker-Token-HMAC",
    ],
)
def test_provider_field_in_sentry_extra_is_redacted(key: str) -> None:
    # Given a provider credential embedded in a captured event.
    canary = "synthetic-provider-secret-781"
    # When the production Sentry hook processes it.
    event = prod.scrub_sentry_event({"extra": {key: canary}}, {})
    # Then the credential is absent, but the event remains useful.
    assert canary not in json.dumps(event)
    assert prod.SENTRY_REDACTION_MARKER in json.dumps(event)


def test_provider_query_is_redacted_from_console() -> None:
    # Given a library diagnostic containing a credential-bearing URL.
    record = logging.makeLogRecord(
        {"msg": "GET https://provider.invalid/file?token=synthetic-query-781"}
    )
    # When the installed console filter runs.
    prod.CredentialURLLogFilter().filter(record)
    # Then no provider token reaches the serialized record.
    assert "synthetic-query-781" not in record.getMessage()


@pytest.mark.parametrize(
    "template",
    [
        "Authorization: Bearer %s",
        "jwt_token=%s",
        "autenticar_procurador_token: %s",
        "X-Anything-Token-Final: %s",
        "GET /file?token=%s",
        "GET https://focusnfe.s3.sa-east-1.amazonaws.com/file?X-Amz-Signature=%s",
    ],
)
def test_formatted_console_messages_scrub_provider_credentials(template: str) -> None:
    canary = "synthetic-formatted-canary"
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(prod.CredentialURLLogFilter())
    logger = logging.getLogger("transport-canary")
    logger.addHandler(handler)
    try:
        logger.warning(template, canary)
    finally:
        logger.removeHandler(handler)
    assert canary not in output.getvalue()
    assert prod.SENTRY_REDACTION_MARKER in output.getvalue()


def test_base64_body_is_not_logged() -> None:
    payload = "YWJj" * 400
    record = logging.makeLogRecord({"msg": "body: %s", "args": (payload,)})
    prod.CredentialURLLogFilter().filter(record)
    assert payload not in record.getMessage()


CANARY = "synthetic-structured-canary-4417"


def production_console(
    emit: Callable[[logging.Logger], None],
    name: str = "transport-privacy-console",
    level: int = logging.WARNING,
) -> str:
    """Real production handler: settings formatter plus the installed filter."""
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(
        logging.Formatter(prod.LOGGING["formatters"]["simple"]["format"])
    )
    for filter_name in prod.LOGGING["handlers"]["console"]["filters"]:
        handler.addFilter(prod.LOGGING["filters"][filter_name]["()"]())
    logger = logging.getLogger(name)
    previous = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        emit(logger)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return output.getvalue()


def captured_events(emit: Callable[[], None]) -> list[Any]:
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        dsn="http://public@example.invalid/1",
        transport=transport,
        before_send=prod.scrub_sentry_event,
        include_local_variables=False,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        emit()
        client.flush()
    client.close()
    return transport.events


@pytest.mark.parametrize(
    "argument",
    [
        [("Authorization", CANARY)],
        [["X-Access-Token", CANARY]],
        (("jwt_token", CANARY),),
        {"headers": [("Authorization", f"Bearer {CANARY}")]},
        {"body": CANARY},
        {"payload": {"nested": CANARY}},
        {"data": [CANARY]},
        f"Bearer {CANARY}",
        f"{{'body': '{CANARY}'}}",
        f'"GET /synthetic.pdf?X-Amz-Signature={CANARY} HTTP/1.1" 200',
        [(b"X-Access-Token", CANARY.encode())],
        [(b"Authorization", CANARY.encode())],
        [(b"Authorization", f"Basic {CANARY}".encode())],
        [(b"jwt_token", CANARY.encode())],
        {b"jwt_token": CANARY.encode()},
        {b"jwt_token": f"a;{CANARY}".encode()},
        f"{[(b'Authorization', CANARY.encode())]}",
    ],
    ids=[
        "header-pair-list",
        "header-pair-nested-list",
        "header-pair-tuple",
        "headers-mapping-of-pairs",
        "body-field",
        "payload-field-nested",
        "data-field-list",
        "bare-bearer",
        "rendered-body-field",
        "logged-signed-query",
        "bytes-pair-x-token",
        "bytes-pair-authorization",
        "bytes-pair-basic",
        "bytes-pair-jwt",
        "bytes-key-mapping",
        "bytes-key-mapping-separator",
        "rendered-bytes-pair",
    ],
)
def test_structured_credentials_are_absent_from_console_output(
    argument: object,
) -> None:
    # Given a diagnostic whose argument carries a credential in structure.
    # When the production console handler formats it.
    output = production_console(
        lambda logger: logger.warning("provider diagnostic %s", argument)
    )
    # Then the credential is absent and the diagnostic itself survives.
    assert CANARY not in output
    assert "provider diagnostic" in output


@pytest.mark.parametrize(
    "pair",
    [
        ("Authorization", f"Basic abc {CANARY}"),
        ("X-Access-Token", f"abc,{CANARY}"),
        ("jwt_token", f"a;{CANARY}"),
        (b"Authorization", f"Basic abc {CANARY}".encode()),
        (b"X-Access-Token", f"abc,{CANARY}".encode()),
    ],
    ids=["space", "comma", "semicolon", "bytes-space", "bytes-comma"],
)
def test_header_pair_value_with_separators_is_redacted_whole(
    pair: tuple[str | bytes, str | bytes],
) -> None:
    # Given a header pair whose value has a separator that text patterns stop at.
    # When the production console handler formats it.
    output = production_console(
        lambda logger: logger.warning("provider diagnostic %s", [pair])
    )
    # Then the whole value is replaced by one intact marker.
    assert CANARY not in output
    assert f"({pair[0]!r}, '[Filtered]')" in output
    assert "[Filtered]]" not in output


@pytest.mark.parametrize("value", [CANARY, CANARY.encode()], ids=["str", "bytes"])
def test_pair_value_is_removed_where_it_appears_again(value: str | bytes) -> None:
    # Given a header pair whose value also appears, unlabelled, in another argument.
    # When the production console handler formats the diagnostic.
    output = production_console(
        lambda logger: logger.warning(
            "provider diagnostic %s then %s",
            [("Authorization", value)],
            f"echo {CANARY}",
        )
    )
    # Then the remembered value is removed from both places.
    assert CANARY not in output
    assert "echo [Filtered]" in output


def upstream_rejection() -> None:
    msg = f"upstream rejected {CANARY}"
    raise ValueError(msg)


def provider_failure(*, explicit: bool) -> None:
    """Raise a wrapper whose cause (or implicit context) mentions the canary."""
    try:
        upstream_rejection()
    except ValueError as error:
        wrapped = "provider call failed"
        if explicit:
            raise RuntimeError(wrapped) from error
        raise RuntimeError(wrapped)  # noqa: B904


@pytest.mark.parametrize("explicit", [True, False], ids=["cause", "context"])
def test_active_secret_is_absent_from_a_rendered_exception_chain(
    explicit: bool,
) -> None:
    # Given an active credential that only an inner exception mentions.
    def emit(logger: logging.Logger) -> None:
        with delivery_secrets(CANARY):
            try:
                provider_failure(explicit=explicit)
            except RuntimeError:
                logger.exception("provider failure")

    # When the production console handler renders the whole chain.
    output = production_console(emit)
    # Then both exceptions are rendered and the credential is not.
    assert "ValueError" in output
    assert "RuntimeError" in output
    assert CANARY not in output


@pytest.mark.parametrize(
    "extra",
    [
        [["Authorization", CANARY]],
        [("X-Access-Token", f"Bearer {CANARY}")],
        f"[('Authorization', '{CANARY}')]",
        f'[["autenticar_procurador_token", "{CANARY}"]]',
        f"Bearer {CANARY}",
    ],
    ids=["pair", "bearer-pair", "rendered-pair", "json-pair", "bare-bearer"],
)
def test_captured_sentry_event_drops_header_pair_credentials(extra: object) -> None:
    # Given header pairs attached to a real captured Sentry event.
    def emit() -> None:
        sentry_sdk.set_extra("headers", extra)
        sentry_sdk.set_extra("unrelated", "retained")
        sentry_sdk.capture_message("provider diagnostic")

    # When the production before_send hook processes it.
    events = captured_events(emit)
    # Then the credential is gone and unrelated context remains.
    assert len(events) == 1
    encoded = json.dumps(events)
    assert CANARY not in encoded
    assert "retained" in encoded


def test_structured_log_message_is_redacted() -> None:
    # Given a mapping logged as the message itself.
    # When the production console handler formats it.
    output = production_console(
        lambda logger: logger.warning(
            {"event": "provider diagnostic", "payload": {"nested": CANARY}}
        )
    )
    # Then the nested provider field is gone and the event survives.
    assert CANARY not in output
    assert "provider diagnostic" in output


def test_active_secret_in_an_exception_note_is_absent_from_console_output() -> None:
    # Given an active credential that only a cause's note mentions (notes are
    # rendered with the chain but are not exception arguments).
    def emit(logger: logging.Logger) -> None:
        with delivery_secrets(CANARY):
            try:
                try:
                    upstream_rejection_with_note()
                except ValueError as error:
                    wrapped = "provider call failed"
                    raise RuntimeError(wrapped) from error
            except RuntimeError:
                logger.exception("provider failure")

    # When the production console handler renders the chain.
    output = production_console(emit)
    # Then the note is rendered without the credential.
    assert "request reference" in output
    assert CANARY not in output


def upstream_rejection_with_note() -> None:
    error = ValueError("upstream rejected")
    error.add_note(f"request reference {CANARY}")
    raise error


def test_structured_fields_in_a_cause_are_absent_from_console_output() -> None:
    # Given a cause whose arguments carry a nested provider field.
    def emit(logger: logging.Logger) -> None:
        try:
            try:
                raise_structured_cause()
            except ValueError as error:
                wrapped = "provider call failed"
                raise RuntimeError(wrapped) from error
        except RuntimeError:
            logger.exception("provider failure")

    # When the production console handler renders the chain.
    output = production_console(emit)
    # Then the cause is rendered without the nested value.
    assert "ValueError" in output
    assert CANARY not in output


def raise_structured_cause() -> None:
    raise ValueError({"payload": {"nested": CANARY}})


def test_header_value_is_removed_from_every_other_event_field() -> None:
    # Given a header pair whose value is repeated in another field.
    def emit() -> None:
        sentry_sdk.set_extra("headers", [["Authorization", CANARY]])
        sentry_sdk.set_extra("note", f"retrying with {CANARY}")
        sentry_sdk.capture_message("provider diagnostic")

    # When the production hook processes the real event.
    encoded = json.dumps(captured_events(emit))
    # Then neither occurrence survives.
    assert CANARY not in encoded
    assert "retrying with" in encoded


def test_captured_sentry_exception_chain_drops_active_secret() -> None:
    # Given an exception chain whose cause mentions an active credential.
    def emit() -> None:
        with delivery_secrets(CANARY):
            try:
                provider_failure(explicit=True)
            except RuntimeError as error:
                sentry_sdk.capture_exception(error)

    # When the real client captures it.
    events = captured_events(emit)
    # Then both exceptions are present and the credential is not.
    encoded = json.dumps(events)
    assert "ValueError" in encoded
    assert "RuntimeError" in encoded
    assert CANARY not in encoded


def test_real_sentry_event_drops_a_jwt_token_extra() -> None:
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        dsn="http://public@example.invalid/1",
        transport=transport,
        before_send=prod.scrub_sentry_event,
        include_local_variables=False,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        scope.set_extra("jwt_token", "synthetic-sentry-jwt")
        scope.set_extra("unrelated", "retained")
        sentry_sdk.capture_message("provider diagnostic")
        client.flush()
    client.close()
    assert len(transport.events) == 1
    encoded = json.dumps(transport.events)
    assert "synthetic-sentry-jwt" not in encoded
    assert "retained" in encoded


def test_response_body_and_auth_header_never_reach_wire_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeFocus((Reply(body=b"synthetic-untrusted-body"),)) as peer:
        permitted_send(
            ProviderTransport("focus", "fake", peer.origin),
            "FC-NFE",
            ProviderRequest(headers=(("Authorization", "synthetic-auth"),)),
        )
    assert "synthetic-untrusted-body" not in caplog.text
    assert "synthetic-auth" not in caplog.text


SIGNATURE = "synthetic-presigned-signature-5521"
OPAQUE = "synthetic-opaque-capability-7731"


@pytest.mark.parametrize("reset", [False, True], ids=["download", "failure"])
def test_presigned_redirect_never_reaches_debug_console_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset: bool
) -> None:
    # Given a real FC-PDF 302 to a presigned storage URL over verified TLS, with
    # urllib3 logging the wire at DEBUG through the production console handler.
    context, ca = tls_context(tmp_path)
    # `opaque` matches no text rule: only the whole URL is a known secret.
    location = (
        f"https://{HOST}/synthetic.pdf?X-Amz-Signature={SIGNATURE}&opaque={OPAQUE}"
    )
    request = ProviderRequest(
        subject=KEY, headers=(("Authorization", "Basic synthetic-pdf-secret"),)
    )
    redirect = Reply(status=302, headers=(("Location", location),))

    def emit(logger: logging.Logger) -> None:
        with (
            FakeFocus(
                (Reply(body=b"%PDF-synthetic", reset=reset),), tls=context
            ) as target,
            FakeFocus((redirect,)) as source,
        ):
            route_storage_host(monkeypatch, target.port)
            client = ProviderTransport("focus", "fake", source.origin, ca)
            # When the download (or its failure) runs and is logged.
            try:
                response = permitted_send(client, "FC-PDF", request)
                logger.warning("provider pdf status %s", response.status)
            except TransportFailure:
                logger.exception("provider pdf failed")
            assert len(target.requests) == 1

    output = production_console(emit, name="urllib3", level=logging.DEBUG)
    # Then the wire was logged, but not the signature or the credential.
    assert "Starting new HTTPS connection" in output
    assert ("provider pdf failed" in output) is reset
    assert SIGNATURE not in output
    assert OPAQUE not in output
    assert "synthetic-pdf-secret" not in output


def test_reviewed_b2_message_list_pair_is_filtered() -> None:
    # Given the review's record whose message is a list of header pairs.
    record = logging.makeLogRecord({"msg": [("Authorization", "Bearer " + CANARY)]})
    # When the production filter runs.
    prod.CredentialURLLogFilter().filter(record)
    # Then the rendered message has no credential.
    assert CANARY not in record.getMessage()


def test_reviewed_b2_sentry_request_header_pairs_are_scrubbed() -> None:
    # Given the review's event with request headers as pairs.
    event = {"request": {"headers": [["Authorization", "Bearer " + CANARY]]}}
    # When the production hook processes it.
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then the credential is gone.
    assert CANARY not in json.dumps(scrubbed)


def test_reviewed_b2_chain_is_scrubbed_in_formatter_output() -> None:
    # Given the review's active secret raised as a cause of a safe wrapper.
    with delivery_secrets(CANARY):
        try:
            provider_failure(explicit=True)
        except RuntimeError:
            record = logging.makeLogRecord(
                {"msg": "provider failure", "exc_info": sys.exc_info()}
            )
        # When the filter runs and a plain Formatter renders the record.
        prod.CredentialURLLogFilter().filter(record)
        output = logging.Formatter().format(record)
    # Then the rendered chain has no credential.
    assert "ValueError" in output
    assert CANARY not in output


FIELDS = ("Authorization", "jwt_token", "autenticar_procurador_token", "X-Access-Token")


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [CANARY, CANARY.encode()], ids=["str", "bytes"])
def test_bytes_keyed_mapping_never_reaches_captured_sentry(
    field: str, value: str | bytes
) -> None:
    # Given a credential under a bytes key, which the SDK serializes as "b'name'".
    def emit() -> None:
        sentry_sdk.set_extra("headers", {field.encode(): value})
        sentry_sdk.set_extra("nested", {"a": {"b": {field.encode(): value}}})
        sentry_sdk.capture_message("provider diagnostic")

    # When a real client delivers it through the production before_send hook.
    events = captured_events(emit)
    # Then the event survives with intact markers and without the credential.
    encoded = json.dumps(events, default=str)
    assert len(events) == 1
    assert CANARY not in encoded
    assert MARKER in encoded
    assert f"{MARKER}]" not in encoded


@pytest.mark.parametrize("scheme", ["Bearer", "Basic"])
def test_active_credential_renders_exactly_one_marker(scheme: str) -> None:
    # Given one credential occurrence that is also registered as an active secret.
    with delivery_secrets(CANARY):
        # When value replacement and the text rules both process the message.
        output = production_console(
            lambda logger: logger.warning("Authorization: %s %s", scheme, CANARY)
        )
    # Then the one occurrence becomes exactly one marker after the scheme.
    assert CANARY not in output
    assert output.count(MARKER) == 1
    assert f"Authorization: {scheme} {MARKER}" in output


@pytest.mark.parametrize("field", FIELDS)
def test_rendered_bytes_pair_renders_exactly_one_marker(field: str) -> None:
    # Given an f-string that rendered a header pair with a bytes value.
    message = f"headers={[(field, CANARY.encode())]!r}"
    # When it reaches the production console as an argument (scrubbed as an
    # argument, then again as the rendered message).
    output = production_console(lambda logger: logger.warning("probe %s", message))
    # Then the value keeps its b'' quotes around exactly one marker.
    assert CANARY not in output
    assert output.count(MARKER) == 1
    assert f"('{field}', b'{MARKER}')" in output


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Bearer {CANARY}",
        f"Authorization: Basic {CANARY}",
        f"headers={[('Authorization', CANARY.encode())]!r}",
        f"headers={[(b'X-Access-Token', CANARY.encode())]!r}",
        f"{{'jwt_token': '{CANARY}'}}",
        f'{{"autenticar_procurador_token": "Bearer {CANARY}"}}',
        f"jwt_token={CANARY}",
    ],
    ids=["bearer", "basic", "bytes-value", "bytes-pair", "mapping", "json", "assign"],
)
def test_scrubbing_scrubbed_text_changes_nothing(text: str) -> None:
    # Given provider text scrubbed once.
    once = scrub_provider_text(text)
    # When it is scrubbed again, as layered filters do.
    twice = scrub_provider_text(once)
    # Then nothing changes and the credential became exactly one marker.
    assert twice == once
    assert CANARY not in once
    assert once.count(MARKER) == 1


@pytest.mark.parametrize(
    "pair",
    [
        (b"X-Access-Token", f"abc,{CANARY}".encode()),
        (b"jwt_token", f"a;{CANARY}".encode()),
        (b"Authorization", f"Basic abc {CANARY}".encode()),
        ("X-Access-Token", f"abc,{CANARY}"),
        ("jwt_token", f"a;{CANARY}"),
        ("Authorization", f"Basic abc {CANARY}"),
    ],
    ids=[
        "comma",
        "semicolon",
        "basic-space",
        "str-comma",
        "str-semicolon",
        "str-space",
    ],
)
def test_rendered_pair_value_is_redacted_to_its_closing_quote(
    pair: tuple[str | bytes, str | bytes],
) -> None:
    # Given an f-string pair whose quoted value contains a separator.
    message = f"headers={[pair]!r}"
    # When the text fallback, with no structure left, renders it.
    output = production_console(lambda logger: logger.warning(message))
    # Then no fragment after the separator survives, and one marker remains.
    assert CANARY not in output
    assert "abc" not in output.split("headers=", 1)[1]
    assert output.count(MARKER) == 1


@pytest.mark.parametrize("key", ["recovery_code", "password1", "body"])
def test_bytes_keyed_sentry_credential_field_is_redacted(key: str) -> None:
    # Given a non-provider credential field under a bytes key.
    def emit() -> None:
        sentry_sdk.set_extra("form", {key.encode(): CANARY, b"unrelated": "kept"})
        sentry_sdk.capture_message("form diagnostic")

    # When a real client delivers it through the production hook.
    encoded = json.dumps(captured_events(emit), default=str)
    # Then the credential is gone and the unrelated field remains.
    assert CANARY not in encoded
    assert "kept" in encoded


@pytest.mark.parametrize("value", [CANARY, CANARY.encode()], ids=["str", "bytes"])
def test_bytes_keyed_mapping_value_is_removed_where_it_appears_again(
    value: str | bytes,
) -> None:
    # Given a bytes-keyed credential mapping and the same value elsewhere.
    def emit(logger: logging.Logger) -> None:
        logger.warning("headers=%s retry=%s", {b"Authorization": value}, CANARY)

    # When the production console renders both arguments.
    output = production_console(emit)
    # Then the mapping value was collected and both occurrences are markers.
    assert CANARY not in output
    assert output.count(MARKER) == 2


@pytest.mark.parametrize("sink", ["console", "sentry"])
@pytest.mark.parametrize("rendering", ["str", "bytes", "json"])
@pytest.mark.parametrize("separator", [",", ";", " "])
@pytest.mark.parametrize("prefix", [MARKER, "abc'\"", "abc\\'\"", "abc\\\\'\""])
def test_quoted_credentials_have_escape_aware_boundaries(
    sink: str, rendering: str, separator: str, prefix: str
) -> None:
    # Given a rendered credential containing markers, quotes or escaped slashes.
    value = prefix + separator + CANARY
    if rendering == "json":
        headers = json.dumps({"X-Access-Token": value})
    elif rendering == "bytes":
        headers = repr([(b"X-Access-Token", value.encode())])
    else:
        headers = repr([("X-Access-Token", value)])
    message = f"provider diagnostic headers={headers} outcome=refused"
    # When the real sink consumes the already-rendered value.
    if sink == "console":
        output = production_console(lambda logger: logger.warning(message))
    else:

        def emit() -> None:
            sentry_sdk.capture_message(message)

        events = captured_events(emit)
        assert len(events) == 1
        output = json.dumps(events)
    # Then the whole value is gone without consuming the following diagnostic.
    assert CANARY not in output
    assert "provider diagnostic" in output
    assert "outcome=refused" in output
    assert output.count(MARKER) == 1
    assert scrub_provider_text(output) == output


@pytest.mark.parametrize("sink", ["console", "sentry"])
@pytest.mark.parametrize("echo_first", [False, True])
@pytest.mark.parametrize(
    ("field", "token"),
    [
        (
            "access_token",
            "eyJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJydjAxMy1hY2Nlc3MifQ.n4a_xT19QMJ9bU8s",
        ),
        (
            "jwt_token",
            "eyJhbGciOiJSUzI1NiJ9.eyJqdGkiOiJydjAxMy1qd3QifQ.N8oQjfcJ9C_tlT3b",
        ),
        ("autenticar_procurador_token", "701c5703-4bf3-4e7a-a88b-0dce8a001313"),
    ],
)
def test_nested_credential_leaves_are_removed_from_unlabelled_echoes(
    sink: str, echo_first: bool, field: str, token: str
) -> None:
    # Given the inherited token grammars in nested fields and unlabelled echoes.
    headers = {field.encode(): {"level2": [token.encode(), {"value": token}]}}
    pairs: list[tuple[str, object]] = [("headers", headers), ("echo", token)]
    if echo_first:
        pairs.reverse()
    context = dict(pairs)
    # When the production formatter or real Sentry client delivers the diagnostic.
    if sink == "console":
        output = production_console(
            lambda logger: logger.warning("provider diagnostic %s", context)
        )
    else:

        def emit() -> None:
            for key, value in context.items():
                sentry_sdk.set_extra(key, value)
            sentry_sdk.capture_message("provider diagnostic")

        events = captured_events(emit)
        assert len(events) == 1
        output = json.dumps(events)
    # Then discovery precedes replacement across the whole diagnostic.
    assert token not in output
    assert "provider diagnostic" in output
    assert MARKER in output


@pytest.mark.parametrize("sink", ["console", "sentry"])
@pytest.mark.parametrize("kind", ["str", "bytes"])
@pytest.mark.parametrize(
    ("field", "token"),
    [
        (
            "access_token",
            "eyJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJydjAxMy1hY2Nlc3MifQ.n4a_xT19QMJ9bU8s",
        ),
        (
            "jwt_token",
            "eyJhbGciOiJSUzI1NiJ9.eyJqdGkiOiJydjAxMy1qd3QifQ.N8oQjfcJ9C_tlT3b",
        ),
        ("autenticar_procurador_token", "701c5703-4bf3-4e7a-a88b-0dce8a001313"),
    ],
)
def test_deep_only_credential_leaf_cannot_escape_through_echo(
    sink: str, kind: str, field: str, token: str
) -> None:
    # Given RV5's deeper leaf, without a second shallow credential to mask it.
    leaf = token.encode() if kind == "bytes" else token
    headers = {field.encode(): {"level2": [{"value": leaf}]}}
    # When the real console handler or captured Sentry client processes it.
    if sink == "console":
        output = production_console(
            lambda logger: logger.warning(
                "deep-only headers=%s echo=%s request=R923", headers, token
            )
        )
    else:

        def emit() -> None:
            sentry_sdk.set_extra("headers", headers)
            sentry_sdk.set_extra("echo", token)
            sentry_sdk.set_extra("request_id", "R923")
            sentry_sdk.capture_message("deep-only")

        events = captured_events(emit)
        assert len(events) == 1
        assert events[0].get("extra", {}).get("request_id") == "R923"
        output = json.dumps(events)
        assert prod.scrub_sentry_event(events[0], {}) == events[0]
    # Then discovery removed both occurrences, retaining the useful context.
    assert token not in output
    assert "R923" in output


@pytest.mark.parametrize("sink", ["console", "sentry"])
@pytest.mark.parametrize("field", ["body", "data", "payload"])
@pytest.mark.parametrize("rendering", ["repr", "json", "bytes"])
def test_escaped_body_is_removed_through_its_real_closing_delimiter(
    sink: str, field: str, rendering: str
) -> None:
    # Given RV5's escaped body, including a sibling diagnostic after its delimiter.
    value = "abc'\";" + CANARY
    if rendering == "json":
        body = json.dumps({field: value})
    elif rendering == "bytes":
        body = repr({field.encode(): value.encode()})
    else:
        body = repr({field: value})
    message = f"body-probe {body} status=refused operation=SP-PAG elapsed=17"
    # When either production sink receives already-rendered text.
    if sink == "console":
        output = production_console(lambda logger: logger.warning(message))
        diagnostic = output
    else:

        def emit() -> None:
            sentry_sdk.capture_message(message)

        events = captured_events(emit)
        assert len(events) == 1
        output = json.dumps(events)
        diagnostic = events[0]["message"]
        assert prod.scrub_sentry_event(events[0], {}) == events[0]
    # Then no body fragment survives and the next fields remain useful.
    assert CANARY not in output
    assert "abc" not in diagnostic
    assert "status=refused operation=SP-PAG elapsed=17" in output


@pytest.mark.parametrize("sink", ["console", "sentry"])
@pytest.mark.parametrize("case", range(48))
def test_generated_depth_keys_quotes_escapes_and_separators(
    sink: str, case: int
) -> None:
    # Given a deterministic generator varying independent input dimensions.
    depth = (0, 2, 7, 17)[case % 4]
    field = ("jwt_token", "payload", "X-Access-Token")[(case // 4) % 3]
    separator = (",", ";", " ")[(case // 3) % 3]
    value = (MARKER, "abc'\"", "\\'\"", "\\\\'\"")[case % 4]
    value += separator + CANARY
    key = field.encode() if case % 2 else field
    leaf: object = value.encode() if (case // 2) % 2 else value
    for level in range(depth):
        leaf = {f"level{level}": [leaf]} if level % 2 else (leaf,)
    context: dict[object, object] = {
        "echo": value,
        value.encode(): "retained",
        "headers": {key: leaf},
        "status": "refused",
        "operation": "SP-PAG",
        "elapsed": 17,
    }
    # Half the cases destroy structure with repr before reaching either sink.
    argument = repr(context) if (case // 8) % 2 else context
    # When the real production sink delivers the generated record.
    if sink == "console":
        output = production_console(
            lambda logger: logger.warning("generated %s", argument)
        )
    else:

        def emit() -> None:
            sentry_sdk.set_extra("generated", argument)
            sentry_sdk.capture_message("generated")

        events = captured_events(emit)
        assert len(events) == 1
        output = json.dumps(events)
        assert prod.scrub_sentry_event(events[0], {}) == events[0]
    # Then both raw and represented echoes disappear, not the diagnostics.
    assert CANARY not in output
    assert "refused" in output
    assert "SP-PAG" in output
    assert "17" in output
    assert "retained" in output


@pytest.mark.parametrize("container", [list, tuple, set])
def test_structural_collection_is_complete_before_any_replacement(
    container: type[list[object]] | type[tuple[object, ...]] | type[set[object]],
) -> None:
    # Given a bytes echo/key before a sensitive field containing overlapping leaves.
    short = "synthetic-short-secret"
    long = short + "-private-tail"
    event: Event = {
        "extra": {
            "echo": long.encode(),
            "context": {long.encode(): "retained"},
            "headers": {b"Authorization": container([short, long.encode()])},
            "status": "refused",
        }
    }
    # When the shared production event walker processes raw telemetry.
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then discovery is independent of iteration order, including set elements.
    output = repr(scrubbed)
    assert short not in output
    assert "-private-tail" not in output
    assert "retained" in output
    assert "refused" in output
    assert prod.scrub_sentry_event(scrubbed, {}) == scrubbed


def test_short_secret_does_not_corrupt_the_marker_on_rescrub() -> None:
    # Given a short real field value and a credential that overlaps marker text.
    event: Event = {"extra": {"echo": "xy Filtered", "jwt_token": ["xy", "Filtered"]}}
    # When collected values are replaced and the whole event is scrubbed again.
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then short credentials are not exempt and existing markers are atomic.
    assert scrubbed.get("extra", {})["echo"] == f"{MARKER} {MARKER}"
    assert prod.scrub_sentry_event(scrubbed, {}) == scrubbed


def test_production_sentry_collects_before_sdk_depth_and_breadth_limits() -> None:
    # Given a credential only after SDK breadth/depth limits, echoed early.
    nested: object = {b"jwt_token": CANARY.encode()}
    for _ in range(1200):
        nested = {"level": nested}
    context: dict[str, object] = {f"safe-{index}": "retained" for index in range(110)}
    context["hidden"] = nested
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        **prod.SENTRY_OPTIONS,
        dsn="http://public@example.invalid/1",
        transport=transport,
    )
    # When the real production client serializes and captures the event.
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        scope.set_extra("echo", CANARY)
        scope.set_extra("context", context)
        sentry_sdk.capture_message("unbounded-depth status=refused elapsed=17")
        client.flush()
    client.close()
    # Then the token was discovered before the SDK could discard its label.
    assert len(transport.events) == 1
    output = json.dumps(transport.events)
    assert CANARY not in output
    assert "status=refused elapsed=17" in output


def test_console_discovers_a_credential_beyond_python_repr_depth() -> None:
    # Given an echo before a credential deeper than Python's repr recursion limit.
    nested: object = {b"jwt_token": CANARY.encode()}
    for _ in range(1200):
        nested = {"level": nested}
    context = {"echo": CANARY, "nested": nested, "status": "refused"}
    # When the real production handler renders the entire record.
    output = production_console(
        lambda logger: logger.warning("deep-console %s elapsed=17", context)
    )
    # Then neither recursion nor redaction loses the useful event.
    assert CANARY not in output
    assert "deep-console" in output
    assert "refused" in output
    assert "elapsed=17" in output


def test_cycle_and_shared_alias_do_not_hide_a_credential_leaf() -> None:
    # Given a cycle and a second reference to a sensitive, bytes-keyed subtree.
    headers: dict[object, object] = {b"jwt_token": CANARY.encode()}
    context: dict[str, object] = {"echo": CANARY, "headers": headers, "alias": headers}
    context["cycle"] = context
    # When a raw event is scrubbed without letting a serializer hide its structure.
    event: Event = {"extra": context}
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then both aliases and the echo are safe, and rescrubbing is stable.
    assert CANARY not in repr(scrubbed)
    assert prod.scrub_sentry_event(scrubbed, {}) == scrubbed


@pytest.mark.parametrize(
    "text",
    [
        f"""payload='abc\\";{CANARY}""",
        f"""headers={{'Authorization': ['abc\\";{CANARY}']}} status=refused""",
        f"""payload={{'nested': ['{CANARY}'] status=refused""",
        f"""headers=[(b'jwt_token', b'abc\\\\";{CANARY}')] status=refused""",
    ],
)
def test_malformed_or_nested_rendering_never_exposes_a_body(text: str) -> None:
    # Given malformed or balanced nested data with no trustworthy inner separator.
    # When the real console sink tokenizes the diagnostic.
    output = production_console(lambda logger: logger.warning(text))
    # Then uncertain closing boundaries fail closed, not at an escaped quote.
    assert CANARY not in output
    assert MARKER in output


@pytest.mark.parametrize("sink", ["console", "sentry"])
def test_structured_record_extras_are_discovered_before_rendering(sink: str) -> None:
    # Given a credential only in an extra field, echoed by the formatted message.
    if sink == "console":
        output = production_console(
            lambda logger: logger.warning(
                "extra-probe echo=%s status=refused",
                CANARY,
                extra={"headers": {b"Authorization": {"nested": {CANARY.encode()}}}},
            )
        )
    else:

        def emit() -> None:
            sentry_sdk.set_extra("headers", {b"Authorization": {"nested": {CANARY}}})
            sentry_sdk.capture_message(f"extra-probe echo={CANARY} status=refused")

        events = captured_events(emit)
        assert len(events) == 1
        output = json.dumps(events)
    # Then the shared discovery, not an active-secret context, removes the echo.
    assert CANARY not in output
    assert "extra-probe" in output
    assert "status=refused" in output


@pytest.mark.parametrize(
    "text",
    [
        f"jwt_token={MARKER}{CANARY} status=refused",
        f"Authorization: Bearer {CANARY} status=refused",
        f"body={{'brace': '{{', 'secret': '{CANARY}'}} status=refused",
        f"payload=[{{'private': '{CANARY}'}}] status=refused",
    ],
)
def test_token_boundaries_preserve_the_following_diagnostic(text: str) -> None:
    # Given marker prefixes, bare values and quoted container delimiters.
    # When the real console handler parses the rendered value.
    output = production_console(lambda logger: logger.warning(text))
    # Then only the complete value is removed, not the next diagnostic.
    assert CANARY not in output
    assert "status=refused" in output
    assert scrub_provider_text(output) == output


@pytest.mark.parametrize("container", [list, tuple, set, frozenset])
def test_collected_credential_is_removed_from_unlabelled_containers(
    container: type,
) -> None:
    # Given unlabelled container elements and a later sensitive field.
    event: Event = {
        "extra": {
            "echo": container([CANARY.encode(), "retained"]),
            "headers": {b"jwt_token": CANARY},
            "status": "refused",
        }
    }
    # When the shared raw-event walker handles every supported container.
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then all aliases are gone without discarding non-secret elements.
    assert CANARY not in repr(scrubbed)
    assert "retained" in repr(scrubbed)
    assert prod.scrub_sentry_event(scrubbed, {}) == scrubbed


@pytest.mark.parametrize("field", ["body", "data", "payload", "jwt_token"])
def test_sensitive_container_is_replaced_as_a_whole(field: str) -> None:
    # Given body contents, including a private mapping key and non-string leaves.
    event: Event = {
        "extra": {
            "echo": CANARY,
            field: {CANARY.encode(): [19, True, None, "private-field"]},
            "status": "refused",
        }
    }
    # When the event is scrubbed before any rendering.
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then neither body shape nor any body leaf reaches the event sink.
    assert scrubbed.get("extra", {})[field] == MARKER
    assert CANARY not in repr(scrubbed)
    assert "refused" in repr(scrubbed)


@pytest.mark.parametrize(
    "message",
    [
        "Authorization",
        "body=",
        "jwt_token:",
        "Basic",
        "ordinary status=refused",
        'not-a-field "quote"',
    ],
)
def test_incomplete_or_non_sensitive_diagnostic_is_still_delivered(
    message: str,
) -> None:
    # Given a boundary token without a value, or unrelated quoted text.
    def emit() -> None:
        sentry_sdk.capture_message(message)

    # When the real Sentry hook tokenizes it.
    events = captured_events(emit)
    # Then parsing never drops the diagnostic to conceal malformed input.
    assert len(events) == 1
    assert events[0].get("message") == message


@pytest.mark.parametrize("encoding", ["unicode", "escaped-slash"])
@pytest.mark.parametrize("sink", ["console", "sentry"])
def test_decoded_rendered_credential_is_removed_from_raw_echoes(
    encoding: str, sink: str
) -> None:
    # Given a credential known only through an escaped JSON value, plus a raw echo.
    token = CANARY + "/private"
    rendered = json.dumps({"jwt_token": token})
    if encoding == "unicode":
        rendered = rendered.replace("synthetic", r"\u0073ynthetic")
    else:
        rendered = rendered.replace("/", r"\/")
    context = {"rendered": rendered, "echo": token, "status": "refused"}
    # When the real sink discovers credentials in rendered strings.
    if sink == "console":
        output = production_console(
            lambda logger: logger.warning("decoded-value %s", context)
        )
    else:

        def emit() -> None:
            sentry_sdk.set_extra("context", context)
            sentry_sdk.capture_message("decoded-value")

        events = captured_events(emit)
        assert len(events) == 1
        output = json.dumps(events)
    # Then discovery uses the actual value, not only its escaped representation.
    assert token not in output
    assert "refused" in output


def test_sdk_annotation_paths_are_not_treated_as_credential_data() -> None:
    # Given real SDK depth annotations mirroring a credential-bearing field.
    event: dict[str, object] = {
        "extra": {
            "headers": {b"jwt_token": {"level2": [{"value": CANARY.encode()}]}},
            "echo": CANARY,
            "status": "refused",
            "operation": "SP-PAG",
        },
        "_meta": {"extra": {"headers": {"jwt_token": {"rem": [["!limit", "x"]]}}}},
    }
    # When the production hook scrubs actual values and then SDK annotations.
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then schema keys and diagnostics survive, without the credential echo.
    extra = scrubbed.get("extra", {})
    assert extra.get("echo") == MARKER
    assert extra.get("status") == "refused"
    assert extra.get("operation") == "SP-PAG"
    assert CANARY not in repr(scrubbed)
    assert prod.scrub_sentry_event(scrubbed, {}) == scrubbed


@pytest.mark.parametrize("artifact", ["signed-url", "base64"])
@pytest.mark.parametrize("sink", ["console", "sentry"])
def test_unlabelled_private_artifact_is_removed_before_serialization(
    artifact: str, sink: str
) -> None:
    # Given unregistered private data without a credential/body field label.
    private_path = "private-object-7251.pdf"
    value = (
        f"https://storage.invalid/{private_path}?X-Amz-Signature={CANARY}"
        if artifact == "signed-url"
        else "YWJj" * 400
    )
    message = f"artifact-download {value} status=refused elapsed=17"
    # When the actual production sink receives the complete, untruncated value.
    if sink == "console":
        output = production_console(lambda logger: logger.warning(message))
    else:
        transport = _CaptureTransport()
        client = sentry_sdk.Client(
            **prod.SENTRY_OPTIONS,
            dsn="http://public@example.invalid/1",
            transport=transport,
        )
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            sentry_sdk.capture_message(message)
            client.flush()
        client.close()
        assert len(transport.events) == 1
        output = json.dumps(transport.events)
    # Then even a truncated prefix or the signed object's path cannot escape.
    assert private_path not in output
    assert CANARY not in output
    assert ("YWJj" * 16) not in output
    assert "artifact-download" in output
    assert "status=refused elapsed=17" in output


@pytest.mark.parametrize("authority", ["[Filtered]", "[not-an-ip]", "[unterminated"])
def test_malformed_url_is_filtered_without_dropping_the_event(authority: str) -> None:
    # Given a URL whose authority is invalid, including a previously scrubbed host.
    url = f"https://{authority}/{CANARY}"

    def emit() -> None:
        sentry_sdk.set_extra("target", url)
        sentry_sdk.set_extra("echo", f"target {url} refused")
        sentry_sdk.capture_message("malformed-url status=refused")

    # When the real Sentry hook parses diagnostic URLs.
    with sentry_sdk.isolation_scope() as scope:
        scope.clear()
        sentry_sdk.get_current_scope().clear()
        events = captured_events(emit)
    # Then the whole invalid URL is hidden, but the event and context survive.
    assert len(events) == 1
    assert CANARY not in json.dumps(events)
    assert events[0]["message"] == "malformed-url status=refused"
    assert events[0]["extra"]["target"] == MARKER
    assert events[0]["extra"]["echo"] == f"target {MARKER} refused"
    assert prod.scrub_sentry_event(events[0], {}) == events[0]


@pytest.mark.parametrize("scalar", [19, 17.5, True, False, None])
@pytest.mark.parametrize("mode", ["before-send-only", "production-options"])
def test_sdk_limited_body_scalars_do_not_become_credentials(
    scalar: float | bool | None, mode: str
) -> None:
    # Given a body scalar deep enough for the SDK to replace it with repr.
    request_id = f"request-{scalar!r}-R923"
    context = {
        "request": {"body": {"private": [scalar, CANARY]}},
        "echo": CANARY,
        "request_id": request_id,
        "elapsed_ms": 17,
        "status": "refused",
    }

    def emit() -> None:
        sentry_sdk.set_extra("context", context)
        sentry_sdk.capture_message("scalar-context")

    # When either supported real SDK configuration delivers the event.
    if mode == "before-send-only":
        events = captured_events(emit)
    else:
        transport = _CaptureTransport()
        client = sentry_sdk.Client(
            **prod.SENTRY_OPTIONS,
            dsn="http://public@example.invalid/1",
            transport=transport,
        )
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            emit()
            client.flush()
        client.close()
        events = transport.events
    # Then real string credentials disappear without inventing scalar secrets.
    assert len(events) == 1
    assert CANARY not in json.dumps(events)
    captured = events[0].get("extra", {}).get("context")
    assert isinstance(captured, dict)
    assert captured["request_id"] == request_id
    assert captured["elapsed_ms"] == 17
    assert captured["status"] == "refused"
    assert prod.scrub_sentry_event(events[0], {}) == events[0]


@pytest.mark.parametrize("credential", ["19", "True", "None"])
def test_sdk_limited_string_credentials_are_not_exempt(credential: str) -> None:
    # Given a real short string credential, not an SDK-represented scalar.
    def emit() -> None:
        sentry_sdk.set_extra(
            "headers", {"jwt_token": {"level2": [{"value": credential}]}}
        )
        sentry_sdk.set_extra("echo", f"alias-{credential}-R923")
        sentry_sdk.capture_message("string-credential")

    # When the SDK represents the deep leaf before the hook runs.
    events = captured_events(emit)
    # Then parsing its representation still discovers the genuine credential.
    assert len(events) == 1
    assert credential not in events[0]["extra"]["echo"]
    assert "R923" in events[0]["extra"]["echo"]


def test_sdk_non_limit_annotations_do_not_exempt_a_credential() -> None:
    # Given a numeric string without SDK depth-limit evidence.
    event: dict[str, object] = {
        "extra": {"jwt_token": "19", "echo": "alias-19-R923"},
        "_meta": {"extra": {"jwt_token": {"": {"rem": [["!size", "x"]]}}}},
    }
    # When the production hook sees an unrelated annotation.
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then that annotation cannot turn a real credential into a safe number.
    echo = scrubbed.get("extra", {}).get("echo")
    assert isinstance(echo, str)
    assert "19" not in echo
    assert "R923" in echo


def test_sdk_malformed_limited_literal_is_still_a_credential() -> None:
    # Given an SDK-limited value that is not a complete Python literal.
    value = f"not a complete literal; {CANARY}"
    event: dict[str, object] = {
        "extra": {"jwt_token": value, "echo": value, "status": "refused"},
        "_meta": {"extra": {"jwt_token": {"": {"rem": [["!limit", "x"]]}}}},
    }
    # When scalar recovery cannot parse it.
    scrubbed = prod.scrub_sentry_event(event, {})
    # Then normal credential discovery still removes it without losing the event.
    assert CANARY not in repr(scrubbed)
    assert scrubbed.get("extra", {}).get("status") == "refused"


CROSSING_HEAD = "rv6-crossing-private-head-D1709"
CROSSING_TAIL = "rv6-crossing-private-tail-D1709"
CROSSING_DIAGNOSTICS = (
    "status=503",
    "operation=SP-PAG",
    "elapsed_ms=37",
    "request_id=RV6-901",
)


def crossing_output(
    sink: str, message: str, extras: Mapping[str, str]
) -> tuple[str, list[Any]]:
    """Deliver one record through a real sink and return its text and events."""
    if sink == "console":
        output = production_console(
            lambda logger: logger.warning(message, extra=dict(extras)),
            name="transport-privacy-crossing",
        )
        return output, []

    def emit() -> None:
        for name, value in extras.items():
            sentry_sdk.set_extra(name, value)
        sentry_sdk.capture_message(message)

    if sink == "before-send-only":
        events = captured_events(emit)
    else:
        transport = _CaptureTransport()
        client = sentry_sdk.Client(
            **prod.SENTRY_OPTIONS,
            dsn="http://public@example.invalid/1",
            transport=transport,
        )
        with sentry_sdk.isolation_scope() as scope:
            scope.clear()
            scope.set_client(client)
            sentry_sdk.get_current_scope().clear()
            emit()
            client.flush()
        client.close()
        events = transport.events
    return json.dumps(events), events


@pytest.mark.parametrize("sink", ["console", "before-send-only", "production-options"])
@pytest.mark.parametrize("field", ["body", "payload", "jwt_token"])
def test_known_value_crossing_a_quoted_credential_leaves_no_private_tail(
    sink: str, field: str
) -> None:
    # Given a known value that starts before a quoted credential and ends inside it.
    known = f"{CROSSING_HEAD} {field}='abc"
    message = (
        f"{known};{CROSSING_TAIL}' "
        "status=503 operation=SP-PAG elapsed_ms=37 request_id=RV6-901"
    )
    # When a real sink delivers the record.
    output, events = crossing_output(sink, message, {"jwt_token": known})
    # Then the union of both ranges is gone and the diagnostics survive.
    assert output
    assert CROSSING_HEAD not in output
    assert CROSSING_TAIL not in output
    assert MARKER in output
    for diagnostic in CROSSING_DIAGNOSTICS:
        assert diagnostic in output
    if sink == "console":
        assert scrub_provider_text(output) == output
    else:
        assert len(events) == 1
        assert prod.scrub_sentry_event(events[0], {}) == events[0]


def test_crossing_ranges_merge_into_exactly_their_union() -> None:
    # Given a known value crossing into a quoted credential, with context around.
    known = "prefix body='abc"
    text = f"keep {known};{CROSSING_TAIL}' status=503 ok"
    # When the shared redactor replaces both ranges.
    once = scrub_text(text, {known})
    # Then one marker covers the union, nothing around it, and rescrubbing is stable.
    assert once == f"keep {MARKER}' status=503 ok"
    assert scrub_text(once, set()) == once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("xx abcd yy", f"xx {MARKER} yy"),
        ("xx ab-cd yy", f"xx {MARKER}-{MARKER} yy"),
        ("xx ab cd yy", f"xx {MARKER} {MARKER} yy"),
    ],
    ids=["adjacent", "separated-by-punctuation", "separated-by-space"],
)
def test_only_touching_ranges_share_a_marker(text: str, expected: str) -> None:
    # Given two known values, touching or separated by one character.
    # When they are replaced, and the result is scrubbed again.
    once = scrub_text(text, {"ab", "cd"})
    # Then touching ranges become one marker and separated ones stay distinct.
    assert once == expected
    assert scrub_text(once, {"ab", "cd"}) == once


CHAIN_WORDS = ("alpha", "bravo", "charlie", "delta", "echo")
CHAIN_FIELDS = ("jwt_token", "access_token", "autenticar_procurador_token")


@pytest.mark.parametrize("windows", [2, 3], ids=["two-values", "chained-values"])
@pytest.mark.parametrize("sink", ["console", "before-send-only", "production-options"])
def test_overlapping_known_values_leave_no_tail_at_real_sinks(
    sink: str, windows: int
) -> None:
    # Given known values that each start inside the previous one and end after it.
    words = [f"rv6{name}D1709" for name in CHAIN_WORDS[: windows + 2]]
    extras = {
        CHAIN_FIELDS[index]: "-".join(words[index : index + 3])
        for index in range(windows)
    }
    message = (
        f"{'-'.join(words)} "
        "status=503 operation=SP-PAG elapsed_ms=37 request_id=RV6-903"
    )
    # When a real sink delivers a record that contains the whole chain.
    output, events = crossing_output(sink, message, extras)
    # Then no word of the chain survives and the diagnostics do.
    assert output
    for word in words:
        assert word not in output
    assert MARKER in output
    for diagnostic in ("status=503", "operation=SP-PAG", "elapsed_ms=37", "RV6-903"):
        assert diagnostic in output
    if sink == "console":
        assert scrub_provider_text(output) == output
    else:
        assert len(events) == 1
        assert prod.scrub_sentry_event(events[0], {}) == events[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("x aaa y", f"x {MARKER} y"),
        ("x aa y aa z", f"x {MARKER} y {MARKER} z"),
    ],
    ids=["overlapping-occurrences", "disjoint-occurrences"],
)
def test_overlapping_occurrences_of_one_value_are_removed_whole(
    text: str, expected: str
) -> None:
    # Given one known value that occurs overlapping itself, or twice apart.
    # When it is replaced, and the result is scrubbed again.
    once = scrub_text(text, {"aa"})
    # Then overlapping occurrences share one marker and separate ones do not.
    assert once == expected
    assert scrub_text(once, {"aa"}) == once


def test_union_that_consumes_a_closing_quote_loses_later_diagnostics_on_rescrub() -> (
    None
):
    # Given a known value that crosses the closing quote of a quoted credential.
    known = f"c' {CROSSING_TAIL}"
    text = f"body='abc' {CROSSING_TAIL} status=503 operation=SP-PAG"
    # When the union removes that quote and the result is scrubbed again.
    once = scrub_text(text, {known})
    twice = scrub_text(once, set())
    # Then the first pass keeps the diagnostics and leaks nothing, the second reads
    # the unterminated body to the end and drops them (privacy-safe), and it is stable.
    assert once == f"body='{MARKER} status=503 operation=SP-PAG"
    assert twice.startswith("body=")
    assert MARKER in twice
    for secret in ("abc", CROSSING_TAIL):
        assert secret not in once
        assert secret not in twice
    for diagnostic in ("status=503", "operation=SP-PAG"):
        assert diagnostic not in twice
    assert scrub_text(twice, set()) == twice
