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


class TestBookListing:
    def test_search_treats_percent_and_underscore_literally(self, client, make_book):
        percent = make_book(title="100% Pure")
        underscore = make_book(title="snake_case")
        make_book(title="Plain")
        assert ids(client.get("/books", params={"q": "%"})) == [percent["id"]]
        assert ids(client.get("/books", params={"q": "_"})) == [underscore["id"]]

    def test_min_price_above_max_price_matches_nothing(self, client, make_book):
        make_book(price_cents=500)
        body = client.get("/books", params={"min_price": 600, "max_price": 400}).json()
        assert (body["items"], body["total"]) == ([], 0)


class TestPatchBook:
    def test_explicit_null_is_rejected_and_changes_nothing(self, client, make_book):
        book = make_book()
        assert client.patch(f"/books/{book['id']}", json={"title": None}).status_code == 422
        assert client.get(f"/books/{book['id']}").json() == book

    def test_empty_body_changes_nothing(self, client, make_book):
        book = make_book()
        response = client.patch(f"/books/{book['id']}", json={})
        assert (response.status_code, response.json()) == (200, book)


class TestOrderAllOrNothing:
    def stock_of(self, client, book):
        return client.get(f"/books/{book['id']}").json()["stock"]

    def test_forbidden_order_leaves_every_book_untouched(self, client, make_member, make_book):
        public = make_book(stock=5)
        restricted = make_book(stock=5, restricted=True)
        items = [{"book_id": public["id"], "quantity": 2}, {"book_id": restricted["id"], "quantity": 1}]
        response = client.post("/orders", json={"member_id": make_member()["id"], "items": items})
        assert response.status_code == 403
        assert (self.stock_of(client, public), self.stock_of(client, restricted)) == (5, 5)

    def test_short_book_listed_after_available_ones_leaves_all_untouched(self, client, make_member, make_book):
        plenty = [make_book(stock=5) for _ in range(3)]
        scarce = make_book(stock=1)
        items = [{"book_id": b["id"], "quantity": 2} for b in [*plenty, scarce]]
        response = client.post("/orders", json={"member_id": make_member()["id"], "items": items})
        assert response.status_code == 409
        assert [self.stock_of(client, b) for b in [*plenty, scarce]] == [5, 5, 5, 1]


class TestLateFeePricing:
    def test_late_fee_uses_the_price_at_return_time(self, client, clock, make_member, make_book):
        book = make_book(price_cents=10_000)
        loan = client.post("/loans", json={"member_id": make_member()["id"], "book_id": book["id"]}).json()
        client.patch(f"/books/{book['id']}", json={"price_cents": 50})
        clock.advance(days=24)  # 10 days late: 250 uncapped, capped at the new price
        assert client.post(f"/loans/{loan['id']}/return").json()["late_fee_cents"] == 50


class TestResponseFormat:
    @pytest.mark.parametrize(
        "method, path, status",
        [("get", "/books/9999", 404), ("get", "/members/9999/stats", 404), ("post", "/orders/9999/pay", 404)],
    )
    def test_errors_carry_a_detail_message(self, client, method, path, status):
        response = getattr(client, method)(path)
        assert response.status_code == status
        assert list(response.json()) == ["detail"] and response.json()["detail"]

    def test_datetimes_are_naive_iso_8601(self, client, make_member):
        created_at = make_member()["created_at"]
        assert created_at == "2026-01-01T12:00:00"


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
