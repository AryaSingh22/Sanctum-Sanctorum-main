"""Order operations: placing, paying and cancelling purchases."""
from datetime import datetime
from typing import Dict, List, NamedTuple

from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError
from app.models import Member, MemberTier, Order, OrderItem, OrderStatus
from app.schemas import OrderCreate, OrderItemIn
from app.services.books import get_books, return_stock, take_stock
from app.services.members import ensure_can_access_restricted, get_member

# Percentage discount granted by each membership tier.
TIER_DISCOUNT_PERCENT: Dict[str, int] = {
    MemberTier.APPRENTICE.value: 0,
    MemberTier.ADEPT.value: 5,
    MemberTier.MASTER.value: 10,
    MemberTier.SUPREME.value: 15,
}

# Extra discount when the total quantity across all items reaches the threshold.
BULK_QUANTITY_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5


def calculate_discount_percent(member: Member, total_quantity: int) -> int:
    """Tier discount, plus the bulk discount when total quantity >= threshold."""
    percent = TIER_DISCOUNT_PERCENT[member.tier]
    if total_quantity >= BULK_QUANTITY_THRESHOLD:
        percent += BULK_DISCOUNT_PERCENT
    return percent


class OrderPricing(NamedTuple):
    subtotal_cents: int
    discount_percent: int
    discount_cents: int
    total_cents: int


def price_order(member: Member, items: List[OrderItem]) -> OrderPricing:
    """Subtotal, discount (rounded down) and total for an order's items."""
    subtotal = sum(item.line_total_cents for item in items)
    percent = calculate_discount_percent(member, sum(item.quantity for item in items))
    discount = subtotal * percent // 100
    return OrderPricing(subtotal, percent, discount, subtotal - discount)


def reserve_stock(db: Session, lines: List[OrderItemIn]) -> None:
    """Take every line's quantity out of stock, or raise 409 and undo the lines already taken."""
    for line in lines:
        if not take_stock(db, line.book_id, line.quantity):
            db.rollback()
            raise ConflictError(f"Not enough stock for book: {line.book_id}")


def create_order(db: Session, data: OrderCreate, now: datetime) -> Order:
    """Place a pending order and reserve stock.

    Checks, in order (422 for empty items / bad quantity / duplicate books is done by the schema):
    1. 404 member not found; 404 any book not found
    2. 403 any book restricted and member tier below master
    3. 409 any book has insufficient stock (all-or-nothing: nothing is changed)
    Then stock is decremented for every item and prices are snapshotted.
    Pricing: discount_cents = subtotal * percent // 100; total = subtotal - discount.
    """
    member = get_member(db, data.member_id)
    books = get_books(db, [line.book_id for line in data.items])
    if any(book.restricted for book in books.values()):
        ensure_can_access_restricted(member)
    reserve_stock(db, data.items)

    items = [
        OrderItem(book_id=line.book_id, quantity=line.quantity, unit_price_cents=books[line.book_id].price_cents)
        for line in data.items
    ]
    pricing = price_order(member, items)
    order = Order(
        member_id=member.id,
        status=OrderStatus.PENDING.value,
        items=items,
        subtotal_cents=pricing.subtotal_cents,
        discount_percent=pricing.discount_percent,
        discount_cents=pricing.discount_cents,
        total_cents=pricing.total_cents,
        created_at=now,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, order_id: int) -> Order:
    """Return an order by id, or raise 404."""
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found")
    return order


def pay_order(db: Session, order_id: int) -> Order:
    """Mark a pending order as paid. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise ConflictError(f"Cannot pay an order that is {order.status}")
    order.status = OrderStatus.PAID.value
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a pending order and restore the reserved stock. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise ConflictError(f"Cannot cancel an order that is {order.status}")
    order.status = OrderStatus.CANCELLED.value
    for item in order.items:
        return_stock(db, item.book_id, item.quantity)
    db.commit()
    db.refresh(order)
    return order
