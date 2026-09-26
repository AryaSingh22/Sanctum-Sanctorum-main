"""Edge cases beyond the provided suite: choices SPEC leaves open, and inputs it does not mention."""


def ids(response) -> list:
    return [book["id"] for book in response.json()["items"]]


class TestTitleOrdering:
    def test_title_sort_ignores_case(self, client, make_book):
        banana = make_book(title="Banana")
        apple = make_book(title="apple")
        cherry = make_book(title="Cherry")
        assert ids(client.get("/books", params={"sort": "title"})) == [apple["id"], banana["id"], cherry["id"]]
        assert ids(client.get("/books", params={"sort": "-title"})) == [cherry["id"], banana["id"], apple["id"]]

    def test_top_books_tie_break_ignores_case(self, client, make_member, make_book):
        banana = make_book(title="Banana")
        apple = make_book(title="apple")
        body = {"member_id": make_member()["id"], "items": [{"book_id": banana["id"], "quantity": 1}, {"book_id": apple["id"], "quantity": 1}]}
        order = client.post("/orders", json=body).json()
        client.post(f"/orders/{order['id']}/pay")
        assert [row["title"] for row in client.get("/reports/top-books").json()] == ["apple", "Banana"]
