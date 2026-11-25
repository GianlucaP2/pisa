"""Found by Schemathesis (`scripts/ci/api_fuzz.py`): the /.well-known documents answer 404 when Pisa publishes
nothing (no PISA_AGENT_URL), and the key directory has its own media type; neither was in the OpenAPI
document."""

import pytest

from pisa.domain import identity


@pytest.mark.db
def test_the_well_known_documents_answer_as_documented(client) -> None:  # type: ignore[no-untyped-def]
    paths = client.app.openapi()["paths"]
    for path in (identity.DIRECTORY_PATH, identity.CARD_PATH, identity.UCP_PROFILE_PATH):
        answer = client.get(path)
        documented = paths[path]["get"]["responses"]
        assert str(answer.status_code) in documented, (path, answer.status_code)
        media_type = answer.headers["content-type"].split(";")[0]
        assert media_type in documented[str(answer.status_code)]["content"], (path, media_type)
        assert "application/json" in documented["404"]["content"]
    directory = paths[identity.DIRECTORY_PATH]["get"]["responses"]["200"]["content"]
    assert list(directory) == [identity.DIRECTORY_TYPE]
