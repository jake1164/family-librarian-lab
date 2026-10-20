"""Release permission stays default off, including ordinary clean-books AI reconfiguration."""
from family_librarian_lab.api import ApiResponse, FamilyLibrarianApi


def test_configure_gateway_always_sends_the_default_off_release_switch(monkeypatch):
    api = FamilyLibrarianApi("http://example.test")
    sent = []
    monkeypatch.setattr(api, "_request", lambda method, path, **kwargs: sent.append((method, path, kwargs)) or ApiResponse(200, {}))
    api.configure_model_gateway(endpoint_url="http://model.test", api_key=None, roles=[])
    assert sent[0][2]["json_body"]["allowModelHoldRelease"] is False
    sent.clear()
    api.configure_model_gateway(endpoint_url="http://model.test", api_key=None, roles=[], allow_model_hold_release=True)
    assert sent[0][2]["json_body"]["allowModelHoldRelease"] is True


def test_explicit_setter_preserves_existing_endpoint_cloud_and_roles(monkeypatch):
    api = FamilyLibrarianApi("http://example.test")
    saved = {"endpointUrl": "http://model.test", "allowCloud": True, "roles": [{"role": "ArtifactValidationDecision"}]}
    sent = []
    def request(method, path, **kwargs):
        sent.append((method, path, kwargs))
        return ApiResponse(200, saved)
    monkeypatch.setattr(api, "_request", request)
    assert api.set_model_hold_release(True).status == 200
    assert sent[0][:2] == ("GET", "/api/v1/admin/model-gateway/")
    assert sent[1][2]["json_body"] == {**saved, "allowModelHoldRelease": True}
