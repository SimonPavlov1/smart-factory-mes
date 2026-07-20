from sqlalchemy.orm import Session

from app.models.inventory import InventoryMovement


def record_movement(db: Session, *, direction: str, quantity: float, balance_after: float,
                    component_id: int | None = None, product_id: int | None = None,
                    location: str | None = None, task_id: int | None = None,
                    order_id: int | None = None, actor_user_id: int | None = None,
                    counterparty_user_id: int | None = None, counterparty_role: str | None = None,
                    recipient: str | None = None, note: str | None = None) -> InventoryMovement:
    movement = InventoryMovement(
        component_id=component_id,
        product_id=product_id,
        direction=direction,
        quantity=float(quantity),
        balance_after=float(balance_after),
        location=location,
        task_id=task_id,
        order_id=order_id,
        actor_user_id=actor_user_id,
        counterparty_user_id=counterparty_user_id,
        counterparty_role=counterparty_role,
        recipient=recipient,
        note=note,
    )
    db.add(movement)
    return movement
