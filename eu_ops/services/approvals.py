"""Approval workflow service for processing users."""
import json
from database import db_cursor


def create_approval_request(order_id: int, user_id: int, action_type: str, action_data: dict = None):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO approval_requests (order_id, requested_by, action_type, action_data)
            VALUES (%s, %s, %s, %s)
        """, (order_id, user_id, action_type, json.dumps(action_data) if action_data else None))
        return cur.lastrowid


def list_pending_approvals():
    with db_cursor() as cur:
        cur.execute("""
            SELECT ar.*, pu.username as requested_by_name, pu.full_name as requester_name,
                   o.firstname, o.lastname, o.total, o.order_status_id
            FROM approval_requests ar
            JOIN platform_users pu ON ar.requested_by = pu.id
            LEFT JOIN `order` o ON ar.order_id = o.order_id
            WHERE ar.status = 'pending'
            ORDER BY ar.created_at DESC
        """)
        rows = cur.fetchall()
        for r in rows:
            if r.get("action_data") and isinstance(r["action_data"], str):
                try:
                    r["action_data"] = json.loads(r["action_data"])
                except Exception:
                    pass
        return rows


def approve_request(request_id: int, admin_id: int, notes: str = ""):
    with db_cursor() as cur:
        cur.execute("""
            UPDATE approval_requests
            SET status = 'approved', reviewed_by = %s, admin_notes = %s, reviewed_at = NOW()
            WHERE id = %s AND status = 'pending'
        """, (admin_id, notes, request_id))
        return cur.rowcount > 0


def reject_request(request_id: int, admin_id: int, notes: str = ""):
    with db_cursor() as cur:
        cur.execute("""
            UPDATE approval_requests
            SET status = 'rejected', reviewed_by = %s, admin_notes = %s, reviewed_at = NOW()
            WHERE id = %s AND status = 'pending'
        """, (admin_id, notes, request_id))
        return cur.rowcount > 0


def get_approval(request_id: int):
    with db_cursor() as cur:
        cur.execute("""
            SELECT ar.*, pu.username as requested_by_name,
                   pu2.username as reviewed_by_name
            FROM approval_requests ar
            JOIN platform_users pu ON ar.requested_by = pu.id
            LEFT JOIN platform_users pu2 ON ar.reviewed_by = pu2.id
            WHERE ar.id = %s
        """, (request_id,))
        row = cur.fetchone()
        if row and row.get("action_data") and isinstance(row["action_data"], str):
            try:
                row["action_data"] = json.loads(row["action_data"])
            except Exception:
                pass
        return row
