import json

import pytest
from custom_components.tuya_recordings.lib.native_auth import NativeAppSession
from custom_components.tuya_recordings.lib.native_gateway import (
    COMPOSITE_KEY,
    NativeAppGatewayClient,
    NativeGatewayAuthError,
    NativeGatewayError,
    NativeGatewayProtocolError,
    _body_key,
    _dump_json,
    _encrypt_post_data,
    _sd_status_http_dp,
    _sd_status_lan_dp,
    _sd_status_lan_error_route,
    _sd_status_mqtt_dp,
    _sd_status_schema_id,
    _wire_api_name,
    generate_device_fingerprint,
    native_app_profile,
)


@pytest.mark.parametrize(("identifier", "expected"), [
    (110, "110"), ("247", "247"), (True, ""), (None, ""), (-1, ""),
    (1.5, ""), ("invalid", ""),
])
def test_sd_status_schema_id_matches_android_string_field_coercion(identifier, expected):
    product = {"schemaInfo": {"schema": json.dumps([{"id": identifier, "code": "sd_status"}])}}
    assert _sd_status_schema_id(product) == expected


@pytest.mark.parametrize(("pv", "cadv", "route"), [
    ("1.0", None, "http"), ("2.2", "1.0.2", "mqtt"), ("2.3", "1.1.0", "mqtt"),
    ("2.2", "1.0.1", ""), ("2.1", "1.0.2", "mqtt"), ("1.1", "1.0.2", ""),
    ("2.0", "1.0.2", "mqtt"),
    (None, "1.0.2", ""),
])
def test_sd_status_lan_send_error_uses_verified_pv_branch(pv, cadv, route):
    assert _sd_status_lan_error_route({"pv": pv, "cadv": cadv, "cloudOnline": False}) == route


@pytest.mark.parametrize(("modes", "online", "mqtt", "http"), [
    ([2], True, "", "110"),
    ([1, 2], True, "110", ""),
    ([1, 2], False, "", "110"),
    ([1, 2], None, "", "110"),
    ([0, 1, 2], False, "", "110"),
    ([8, 2], False, "", ""),
    ([0, 8, 1, 2], True, "", ""),
])
def test_sd_status_cloud_routes_do_not_override_lan_or_matter(modes, online, mqtt, http):
    device = {
        "cloudOnline": online, "cadv": "1.0.2",
        "communication": {"dataModel": 1, "communicationModes": [{"type": mode} for mode in modes]},
        "productBean": {"schemaInfo": {"schema": '[{"id":"110","code":"sd_status"}]'}},
    }
    assert (_sd_status_mqtt_dp(device), _sd_status_http_dp(device)) == (mqtt, http)
    assert _sd_status_lan_dp(device) == ("110" if 0 in modes else "")


@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
@pytest.mark.parametrize("result", ["matched", "wrong_version", "wrong_product", "duplicate", "malformed", "failed"])
def test_missing_product_schema_is_versioned_bounded_and_semantic(monkeypatch, profile, result):
    client = NativeAppGatewayClient(session=FakeSession(), app_profile_id=profile)
    devices = []
    for index in range(23):
        detail = {
            "devId": f"camera-{index}", "productId": f"product-{index}", "productVer": "3.1",
            "communication": {"communicationNode": f"camera-{index}", "dataModel": 1,
                              "communicationModes": [{"type": 1, "pv": "2.3"}]},
            "cloudOnline": True, "gatewayVerCAD": "1.0.2",
            "skills": {"p2pType": 2}, "dataPointInfo": {"dps": {"149": False}},
        }
        devices.append(({"devId": detail["devId"]}, detail))
    devices.append(({"devId": "duplicate-camera"}, {**devices[0][1], "devId": "duplicate-camera",
                    "communication": {**devices[0][1]["communication"], "communicationNode": "duplicate-camera"}}))
    calls = []

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        assert (api, version, post["zigbeeGroup"]) == ("thing.m.device.ref.info.list", "6.1", True)
        products = json.loads(post["productIds"])
        calls.append(products)
        if result == "failed":
            raise NativeGatewayError("fixture unavailable")
        if result == "malformed":
            return {}
        rows = [{
            "id": item["pid"] if result != "wrong_product" else "different",
            "productVer": item["ver"] if result != "wrong_version" else "other",
            "schemaInfo": {"schema": '[{"id":"247","code":"sd_status"}]'},
        } for item in products]
        return rows * 2 if result == "duplicate" else rows

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    client._resolve_product_schemas(devices, None)
    assert [len(batch) for batch in calls] == [20, 3]
    assert len({item["pid"] for batch in calls for item in batch}) == 23
    for row, _ in devices:
        assert row["sdSchemaLookup"] == ("matched" if result == "matched" else "unavailable")
        assert row.get("sdStatusMqttDp") == ("247" if result == "matched" else None)


@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
def test_missing_product_version_is_not_fabricated(monkeypatch, profile):
    client = NativeAppGatewayClient(session=FakeSession(), app_profile_id=profile)
    calls = []

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        calls.append(json.loads(post["productIds"]))
        return [{"id": "p1", "schemaInfo": {"schema": '[{"id":"247","code":"sd_status"}]'}}]

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    row = {"devId": "camera"}
    client._resolve_product_schemas([(row, {"devId": "camera", "productId": "p1"})], None)
    assert calls == [[{"pid": "p1"}]]
    assert row["sdSchemaLookup"] == "matched"


def test_tuya_smart_profile_uses_its_own_current_mobile_identity():
    profile = native_app_profile("tuya_smart")

    assert profile.package_name == "com.tuya.smart"
    assert profile.app_version == "7.11.0"
    assert profile.qr_scheme == "tuyaSmart"
    assert profile.composite_key != COMPOSITE_KEY


def test_device_fingerprint_is_unique_urlsafe_and_sdk_sized():
    first = generate_device_fingerprint()
    second = generate_device_fingerprint()

    assert first != second
    assert len(first) == len(second) == 44
    assert all(character.isalnum() or character in "-_" for character in first)


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, result=None, envelope=None):
        self.result = result or {"ok": True}
        self.envelope = envelope
        self.calls = []

    def post(self, url, *, data, headers, timeout):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if self.envelope is not None:
            return FakeResponse(json.dumps(self.envelope))
        key = _body_key(data["requestId"], "0123456789abcdef", COMPOSITE_KEY)
        encrypted = _encrypt_post_data(key, _dump_json({"success": True, "result": self.result}))
        return FakeResponse(json.dumps({"result": encrypted}))


def test_app_session_gateway_call_signs_encrypts_and_decrypts_response():
    session = FakeSession({"cameraInfo": {"id": "p2p-id"}})
    client = NativeAppGatewayClient(region="eu", session=session)

    result = client.call(
        "m.ipc.v4.rtc.config.get",
        "1.0",
        {"devId": "camera-id"},
        NativeAppSession(
            sid="sid-value",
            ecode="0123456789abcdef",
            uid="uid-value",
            partner_identity="partner",
            mobile_mqtts_url="mqtt.example.test",
            device_fingerprint="fingerprint",
        ),
    )

    call = session.calls[0]
    params = call["data"]
    assert call["url"] == "https://a1-eu.lifeaiot.com/api.json"
    assert params["a"] == "m.ipc.v4.rtc.config.get"
    assert params["v"] == "1.0"
    assert params["sid"] == "sid-value"
    assert params["et"] == "3"
    assert len(params["sign"]) == 64
    assert params["postData"] != json.dumps({"devId": "camera-id"})
    assert result == {"cameraInfo": {"id": "p2p-id"}}


def test_thing_api_names_are_rewritten_for_smartlife_wire_calls():
    assert _wire_api_name("thing.m.rtc.session.init") == "smartlife.m.rtc.session.init"
    assert _wire_api_name("m.ipc.v4.rtc.config.get") == "m.ipc.v4.rtc.config.get"


def test_saved_session_requires_complete_native_qr_authorization():
    client = NativeAppGatewayClient(session=FakeSession())

    with pytest.raises(NativeGatewayAuthError, match="incomplete"):
        client.call_with_saved_session("m.ipc.v4.rtc.config.get", "1.0", {}, {"sid": "sid", "ecode": "ecode"})


def test_saved_session_passes_extra_query_parameters(monkeypatch):
    client = NativeAppGatewayClient(session=FakeSession())
    captured = {}

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        captured.update(
            api=api,
            version=version,
            post=post,
            sid=app_session.sid,
            extra_params=extra_params,
        )
        return {"ok": True}

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    result = client.call_with_saved_session(
        "tuya.m.my.group.device.list",
        "1.0",
        {},
        {
            "sid": "sid",
            "ecode": "ecode",
            "uid": "uid",
            "partner_identity": "partner",
            "mobile_mqtts_url": "mqtt.example.test",
            "device_fingerprint": "fingerprint",
        },
        {"gid": "123"},
    )

    assert result == {"ok": True}
    assert captured == {
        "api": "tuya.m.my.group.device.list",
        "version": "1.0",
        "post": {},
        "sid": "sid",
        "extra_params": {"gid": "123"},
    }


def test_device_local_keys_uses_each_home_once(monkeypatch):
    client = NativeAppGatewayClient(session=FakeSession())
    calls = []

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        calls.append((api, version, post, extra_params))
        if api == "tuya.m.location.list":
            return [{"gid": 123}, {"gid": 123}, {"gid": "invalid"}]
        return [
            {
                "devId": "camera-a",
                "localKey": "fresh-a",
                "pv": "2.3",
            },
            {
                "devId": "camera-b",
                "localKey": "fresh-b",
                "moduleMap": {"wifi": {"pv": "invalid"}},
            },
            {"devId": "missing-key"},
        ]

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    keys = client.device_local_keys(
        {
            "sid": "sid",
            "ecode": "ecode",
            "uid": "uid",
            "partner_identity": "partner",
            "mobile_mqtts_url": "mqtt.example.test",
            "device_fingerprint": "fingerprint",
        }
    )

    assert keys == {"camera-a": "fresh-a", "camera-b": "fresh-b"}
    assert calls == [
        ("tuya.m.location.list", "2.1", {}, None),
        ("tuya.m.my.group.device.list", "1.0", {}, {"gid": "123"}),
    ]


def test_device_connection_data_extracts_apk_mqtt_protocol(monkeypatch):
    client = NativeAppGatewayClient(session=FakeSession())

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        if api == "tuya.m.location.list":
            return [{"gid": 123}]
        return [
            {
                "devId": "camera-a",
                "localKey": "fresh-a",
                "protocol_version": "3.5",
                "pv": "2.3",
                "moduleMap": {"wifi": {"pv": "9.9"}},
            }
        ]

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)

    keys, protocols, inventory = client.device_connection_inventory_data(
        {
            "sid": "sid",
            "ecode": "ecode",
            "uid": "uid",
            "partner_identity": "partner",
            "mobile_mqtts_url": "mqtt.example.test",
            "device_fingerprint": "fingerprint",
        }
    )

    assert keys == {"camera-a": "fresh-a"}
    assert protocols == {"camera-a": "2.3"}
    assert inventory == [{
        "devId": "camera-a",
        "name": "camera-a",
        "category": "",
        "productId": "",
        "online": None,
        "sdWakeEvidence": {
            "source": "group", "route": "missing", "provider_two": None,
            "dp149": "missing", "sd_status_schema": False,
        },
    }]


@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
@pytest.mark.parametrize("metadata_available", [True, False])
def test_inventory_joins_product_refs_and_keeps_only_wake_flags(monkeypatch, profile, metadata_available):
    client = NativeAppGatewayClient(session=FakeSession(), app_profile_id=profile)
    calls = []

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        calls.append((api, version, post, extra_params))
        if api == "tuya.m.location.list":
            return [{"gid": 123}]
        if api == "tuya.m.my.group.device.list":
            return [
                {"devId": name, "localKey": "private", "productId": product,
                 "communication": communication, "skills": {"p2pType": 2},
                 "productBean": {"schemaInfo": {"schema": '[{"code":"wireless_awake"}]'}}}
                for name, product, communication in (
                    ("a", "p1", {"communicationNode": "a"}),
                    ("b", "p1", {"communicationNode": "hub"}),
                    ("c", "p2", {}),
                    ("d", "p3", None),
                    ("e", "p2", {"communicationNode": "e"}),
                )
            ]
        if not metadata_available:
            raise NativeGatewayError("metadata unavailable")
        return [
            {"productId": "p1", "configMetas": {"low_power_wakeup": False, "remote_wakeup": None}},
            {"productId": "p2", "configMetas": {}},
            {"productId": "p3", "configMetas": None},
        ]

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    _, _, inventory = client.device_connection_inventory_data({
        "sid": "sid", "ecode": "ecode", "uid": "uid", "partner_identity": "partner",
        "mobile_mqtts_url": "mqtt.example.test", "device_fingerprint": "fingerprint",
    })
    assert calls[-1] == (
        "thing.m.device.product.ref.list", "1.0",
        {"productIds": '["p1","p2","p3"]'}, None,
    )
    assert len(calls) == 3
    expected = [
        "low_power_and_remote", "low_power_and_remote", "none", None, "wireless",
    ] if metadata_available else ["wireless", None, None, None, "wireless"]
    assert [row.get("wakeCapability") for row in inventory] == expected
    assert [row.get("wakeDirect") for row in inventory] == [True, False, True, None, True]
    assert all(not {"configMetas", "localKey", "productBean"} & row.keys() for row in inventory)


@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("refresh_protocols", [True, False])
def test_inventory_resolves_missing_wireless_schema_once_per_home(monkeypatch, profile, available, refresh_protocols):
    client = NativeAppGatewayClient(session=FakeSession(), app_profile_id=profile)
    lookups = []
    camera_ids = ("direct", "hub-child", "different-product")

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        if api == "tuya.m.location.list":
            return [{"gid": 123}, {"gid": 123}]
        if api == "tuya.m.my.group.device.list":
            return [
                {"devId": dev_id, "localKey": "original-key", "category": "sp", "productId": "p1",
                 **({"pv": "2.2"} if refresh_protocols else {})}
                for dev_id in camera_ids
            ]
        if api == "thing.m.device.product.ref.list":
            return []
        if api == "thing.m.device.ref.info.list":
            return []
        if api == "thing.m.device.get":
            assert refresh_protocols
            return None
        assert api == "m.life.app.smart.local.device.list"
        lookups.append((version, post, extra_params))
        if not available:
            raise NativeGatewayError("optional inventory unavailable")
        return {
            "deviceList": [
                {
                    "devId": dev_id, "productId": product, "localKey": "different-key",
                    "communication": {
                        "communicationNode": node, "dataModel": 1,
                        "communicationModes": [
                            {"type": 0, "pv": "3.5"}, {"type": 2, "pv": "2.2"},
                            {"type": 1, "pv": "2.3"},
                        ],
                    },
                    "skills": {"p2pType": 2}, "gatewayVerCAD": "1.0.2", "cloudOnline": True,
                    "dataPointInfo": {"dps": {"149": False}},
                }
                for dev_id, product, node in (
                    ("direct", "p1", "direct"),
                    ("hub-child", "p1", "hub"),
                    ("different-product", "p2", "different-product"),
                )
            ],
            "productList": [
                {"id": "p1", "schemaInfo": {"schema": json.dumps([
                    {"id": "77", "code": "wireless_awake", "type": "bool"},
                    {"id": "78", "code": "offline_show_set", "type": "bool"},
                    {"id": "110", "code": "sd_status"},
                ])}},
            ],
        }

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    keys, protocols, inventory = client.device_connection_inventory_data({
        "sid": "sid", "ecode": "ecode", "uid": "uid", "partner_identity": "partner",
        "mobile_mqtts_url": "mqtt.example.test", "device_fingerprint": "fingerprint",
    }, refresh_protocols=refresh_protocols)
    assert lookups == [("1.1", {"homeId": 123, "groupType": "homeGroup"}, None)]
    assert keys == dict.fromkeys(camera_ids, "original-key")
    expected_pvs = dict.fromkeys(camera_ids, "2.2") if refresh_protocols else {}
    if available:
        expected_pvs.update({"direct": "2.3", "hub-child": "2.3"})
    assert protocols == expected_pvs
    assert [row.get("wakeCapability") for row in inventory] == (
        ["wireless", None, None] if available else [None, None, None]
    )
    assert [row.get("wakeDirect") for row in inventory] == (
        [True, False, None] if available else [None, None, None]
    )
    assert all("wakeOfflineDp" not in row for row in inventory)
    assert [row.get("sdLowPowerWake") for row in inventory] == (
        [True, False, None] if available else [None, None, None]
    )
    assert [row.get("sdStatusMqttDp") for row in inventory] == (
        ["110", None, None] if available else [None, None, None]
    )


@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
@pytest.mark.parametrize("refresh", [False, True])
def test_inventory_only_fetches_details_for_unresolved_cameras(monkeypatch, profile, refresh):
    client = NativeAppGatewayClient(session=FakeSession(), app_profile_id=profile)
    requests = []
    detail_ids = ("group", "home", "recovered", "missing", "wrong-device", "wrong-product", "malformed", "unavailable")

    def row(dev_id, **fields):
        return {"devId": dev_id, "localKey": "fixture-key", "category": "sp", "productId": "p1", **fields}

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        if api == "tuya.m.location.list":
            return [{"gid": 123}, {"gid": 456}]
        if api == "tuya.m.my.group.device.list":
            if extra_params == {"gid": "456"}:
                return [row("missing")]
            return [row("group", pv="2.2"), row("home"),
                    {**row("plug"), "category": "cz"}, *(row(dev_id) for dev_id in detail_ids[2:])]
        if api == "thing.m.device.product.ref.list":
            return []
        if api == "m.life.app.smart.local.device.list":
            return {"deviceList": [row("home", pv="2.3")]}
        if api == "thing.m.device.ref.info.list":
            return []
        assert api == "thing.m.device.get"
        assert (version, extra_params, app_session.sid) == ("4.1", {"gid": "123"}, "sid")
        dev_id = post["devId"]
        requests.append(dev_id)
        if dev_id in {"group", "home"}:
            return row(
                dev_id, pv="2.1",
                communication={"communicationNode": dev_id if dev_id == "group" else "hub"},
                skills={"p2pType": 2}, dataPointInfo={"dps": {"149": False}},
            )
        if dev_id == "unavailable":
            raise NativeGatewayError("optional detail unavailable")
        if dev_id == "malformed":
            return []
        if dev_id == "wrong-device":
            return row("other-device", pv="2.3")
        if dev_id == "wrong-product":
            return {**row(dev_id, pv="2.3"), "productId": "p2"}
        if dev_id == "missing":
            return row(dev_id, communication={"communicationModes": [{"type": 0, "pv": "3.5"}]})
        assert dev_id == "recovered"
        return row(dev_id, communication={"communicationModes": [{"type": 1, "pv": "2.3"}]})

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    _, protocols, inventory = client.device_connection_inventory_data({
        "sid": "sid", "ecode": "ecode", "uid": "uid", "partner_identity": "partner",
        "mobile_mqtts_url": "mqtt.example.test", "device_fingerprint": "fingerprint",
    }, refresh_protocols=refresh)
    assert requests == (list(detail_ids) if refresh else [])
    assert protocols == {"group": "2.2", "home": "2.3", **({"recovered": "2.3"} if refresh else {})}
    evidence = {item["devId"]: (item.get("wakeDirect"), item.get("sdLowPowerWake")) for item in inventory}
    assert evidence["group"] == ((True, True) if refresh else (None, None))
    assert evidence["home"] == ((False, False) if refresh else (None, None))
    assert evidence["missing"] == (None, None)
    rows = {item["devId"]: item for item in inventory}
    if refresh:
        assert rows["group"]["sdWakeEvidence"] == {
            "source": "detail", "route": "direct", "provider_two": True,
            "dp149": "present", "sd_status_schema": False,
        }
        assert rows["home"]["sdWakeEvidence"]["route"] == "hub"
        assert rows["missing"]["sdWakeEvidence"]["provider_two"] is None
        assert rows["missing"]["sdWakeEvidence"]["dp149"] == "missing"
        assert rows["wrong-device"]["sdWakeDetailLookup"] == "invalid_device"
        assert rows["wrong-product"]["sdWakeDetailLookup"] == "product_mismatch"
        assert rows["unavailable"]["sdWakeDetailLookup"] == "failed"
        assert "fixture-key" not in json.dumps([item["sdWakeEvidence"] for item in inventory])


@pytest.mark.parametrize("homes", [None, {}, "invalid"])
def test_device_local_keys_rejects_malformed_home_list(monkeypatch, homes):
    client = NativeAppGatewayClient(session=FakeSession())
    monkeypatch.setattr(
        NativeAppGatewayClient,
        "call",
        lambda self, *args, **kwargs: homes,
    )

    with pytest.raises(NativeGatewayProtocolError, match="home list"):
        client.device_local_keys(
            {
                "sid": "sid",
                "ecode": "ecode",
                "uid": "uid",
                "partner_identity": "partner",
                "mobile_mqtts_url": "mqtt.example.test",
                "device_fingerprint": "fingerprint",
            }
        )


def test_session_invalid_error_maps_to_auth_error():
    client = NativeAppGatewayClient(
        session=FakeSession(envelope={"success": False, "errorCode": "USER_SESSION_INVALID", "errorMsg": "expired"})
    )

    with pytest.raises(NativeGatewayAuthError, match="USER_SESSION_INVALID"):
        client.call(
            "m.ipc.v4.rtc.config.get",
            "1.0",
            {},
            NativeAppSession(
                sid="sid",
                ecode="0123456789abcdef",
                uid="uid",
                partner_identity="partner",
                mobile_mqtts_url="mqtt.example.test",
                device_fingerprint="fingerprint",
            ),
        )


def test_malformed_encrypted_response_is_protocol_error():
    client = NativeAppGatewayClient(session=FakeSession(envelope={"result": "not-base64"}))

    with pytest.raises(NativeGatewayProtocolError):
        client.call(
            "m.ipc.v4.rtc.config.get",
            "1.0",
            {},
            NativeAppSession(
                sid="sid",
                ecode="0123456789abcdef",
                uid="uid",
                partner_identity="partner",
                mobile_mqtts_url="mqtt.example.test",
                device_fingerprint="fingerprint",
            ),
        )
