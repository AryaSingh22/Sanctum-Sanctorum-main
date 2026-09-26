"""GET /members: an optional extra from ASSIGNMENT.md, paginated like GET /books."""
import pytest


class TestListMembers:
    def test_no_members(self, client):
        response = client.get("/members")
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}

    def test_members_are_listed_in_id_order_as_full_objects(self, client, make_member):
        members = [make_member() for _ in range(3)]
        assert client.get("/members").json()["items"] == members

    def test_limit_and_offset_with_total(self, client, make_member):
        members = [make_member() for _ in range(5)]
        body = client.get("/members", params={"limit": 2, "offset": 2}).json()
        assert [m["id"] for m in body["items"]] == [members[2]["id"], members[3]["id"]]
        assert (body["total"], body["limit"], body["offset"]) == (5, 2, 2)

    def test_default_limit_is_20(self, client, make_member):
        for _ in range(21):
            make_member()
        body = client.get("/members").json()
        assert len(body["items"]) == 20
        assert body["total"] == 21

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    def test_out_of_range_pagination_returns_422(self, client, params):
        assert client.get("/members", params=params).status_code == 422
