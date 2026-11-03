import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

PORT = int(os.environ.get("PORT", "5002"))
DATABASE_URL = os.environ["DATABASE_URL"]

def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def initialize_database():
    for attempt in range(30):
        try:
            with connect() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS orders (
                    id BIGSERIAL PRIMARY KEY,
                    customer_name VARCHAR(120) NOT NULL,
                    status VARCHAR(30) NOT NULL DEFAULT 'PLACED',
                    total NUMERIC(12,2) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
                conn.execute("""CREATE TABLE IF NOT EXISTS order_items (
                    id BIGSERIAL PRIMARY KEY,
                    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    product_id BIGINT NOT NULL,
                    product_name VARCHAR(120) NOT NULL,
                    unit_price NUMERIC(12,2) NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    line_total NUMERIC(12,2) NOT NULL)""")
            print("Order database initialized", flush=True)
            return
        except Exception as exc:
            print(f"Database connection attempt {attempt + 1}: {exc}", flush=True)
            time.sleep(2)
    raise RuntimeError("Unable to connect to PostgreSQL")

def serialize_order(row, items):
    return {
        "order_id": f"ORD-{row['id']:06d}", "id": row["id"],
        "customer_name": row["customer_name"], "status": row["status"],
        "total": float(row["total"]), "created_at": row["created_at"].isoformat(),
        "items": [{"product_id": i["product_id"], "product_name": i["product_name"],
                   "unit_price": float(i["unit_price"]), "quantity": i["quantity"],
                   "line_total": float(i["line_total"])} for i in items]
    }

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("Request body is required")
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/order":
            path = "/orders"
        if path == "/health":
            try:
                with connect() as conn: conn.execute("SELECT 1")
                return self.send_json(200, {"status": "ok", "service": "order-api"})
            except Exception:
                return self.send_json(503, {"status": "database unavailable"})
        if path != "/orders":
            return self.send_json(404, {"error": "Not found"})
        with connect() as conn:
            orders = conn.execute("SELECT * FROM orders ORDER BY created_at DESC, id DESC").fetchall()
            result = []
            for order in orders:
                items = conn.execute("SELECT * FROM order_items WHERE order_id=%s ORDER BY id", (order["id"],)).fetchall()
                result.append(serialize_order(order, items))
        return self.send_json(200, {"orders": result})

    def do_POST(self):
        if urlparse(self.path).path not in ("/orders", "/order"):
            return self.send_json(404, {"error": "Not found"})
        try:
            data = self.read_body()
            customer = str(data.get("customer_name", "")).strip()
            product_id = int(data.get("product_id", 0))
            quantity = int(data.get("quantity", 0))
            if not customer or len(customer) > 120:
                raise ValueError("Customer name is required (maximum 120 characters)")
            if product_id <= 0 or quantity <= 0:
                raise ValueError("Valid product_id and quantity are required")
            with connect() as conn:
                product = conn.execute("""SELECT id,name,price,stock FROM products
                    WHERE id=%s FOR UPDATE""", (product_id,)).fetchone()
                if not product:
                    return self.send_json(404, {"error": "Product not found"})
                if product["stock"] < quantity:
                    return self.send_json(409, {"error": f"Insufficient stock. Available: {product['stock']}"})
                line_total = product["price"] * quantity
                order = conn.execute("""INSERT INTO orders(customer_name,total)
                    VALUES(%s,%s) RETURNING *""", (customer,line_total)).fetchone()
                conn.execute("""INSERT INTO order_items
                    (order_id,product_id,product_name,unit_price,quantity,line_total)
                    VALUES(%s,%s,%s,%s,%s,%s)""",
                    (order["id"],product["id"],product["name"],product["price"],quantity,line_total))
                conn.execute("UPDATE products SET stock=stock-%s,updated_at=NOW() WHERE id=%s", (quantity,product_id))
                items = conn.execute("SELECT * FROM order_items WHERE order_id=%s", (order["id"],)).fetchall()
            return self.send_json(201, {"message":"Order placed successfully", "order":serialize_order(order,items)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error":str(exc)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

initialize_database()
print(f"order-api listening on 0.0.0.0:{PORT}", flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
