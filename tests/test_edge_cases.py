"""Edge cases beyond the provided suite: choices SPEC leaves open, and inputs it does not mention."""
import pytest

TOO_BIG = 2**31  # one past the largest value a 32-bit INTEGER column holds


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


class TestLargeNumbers:
    @pytest.mark.parametrize("field", ["price_cents", "stock"])
    def test_book_numbers_beyond_the_column_range_return_422(self, client, make_book, field):
        book = make_book()
        assert client.patch(f"/books/{book['id']}", json={field: TOO_BIG}).status_code == 422
        assert client.patch(f"/books/{book['id']}", json={field: TOO_BIG - 1}).status_code == 200

    def test_order_quantity_beyond_the_column_range_returns_422(self, client, make_member, make_book):
        body = {"member_id": make_member()["id"], "items": [{"book_id": make_book()["id"], "quantity": TOO_BIG}]}
        assert client.post("/orders", json=body).status_code == 422

    def test_order_total_may_exceed_32_bits(self, client, make_member, make_book):
        book = make_book(price_cents=1_000_000, stock=5_000)  # a $10,000 book
        body = {"member_id": make_member()["id"], "items": [{"book_id": book["id"], "quantity": 3_000}]}
        response = client.post("/orders", json=body)
        assert response.status_code == 201
        assert response.json()["subtotal_cents"] == 3_000_000_000
        assert response.json()["total_cents"] == 2_850_000_000  # 5% bulk discount
