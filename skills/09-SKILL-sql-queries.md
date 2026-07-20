# Odoo SQL & Database Queries Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE QUERY PATTERNS

### **1. BASIC SELECTS & FILTERS**
```python
# Simple search
partners = self.env['res.partner'].search([
    ('name', 'ilike', '%ABC%'),
    ('active', '=', True),
])

# Search with operators
operators = {
    '=': 'exact match',
    '!=': 'not equal',
    'like': 'contains (case-sensitive)',
    'ilike': 'contains (case-insensitive)',
    '=like': 'begins with',
    'not like': 'does not contain',
    'in': 'in list',
    'not in': 'not in list',
    '<': 'less than',
    '<=': 'less or equal',
    '>': 'greater than',
    '>=': 'greater or equal',
}

# Examples
sales = self.env['sale.order'].search([
    ('state', '!=', 'cancel'),
    ('amount_total', '>=', 10000),
    ('partner_id.country_id.code', '=', 'US'),  # Nested field
    ('date_order', '>=', '2024-01-01'),
])

# Read specific fields (not all)
order_data = sales.read(['name', 'amount_total', 'partner_id'])
# Returns: [{'id': 1, 'name': 'SO-001', 'amount_total': 50000, 'partner_id': (2, 'ABC Corp')}]

# Get single record
first_order = sales[0] if sales else None
name = first_order.name if first_order else 'N/A'

# Direct SQL (if needed)
self.env.cr.execute("""
    SELECT id, name, amount_total 
    FROM sale_order 
    WHERE state != 'cancel'
    ORDER BY date_order DESC
    LIMIT 10
""")
records = self.env.cr.fetchall()  # [(1, 'SO-001', 50000), ...]
```

### **2. LOGICAL OPERATORS (AND, OR, NOT)**
```python
# AND (default, implicit)
invoices = self.env['account.move'].search([
    ('move_type', '=', 'out_invoice'),
    ('state', '=', 'posted'),
    ('amount_total', '>', 0),
])
# SQL: WHERE move_type = 'out_invoice' AND state = 'posted' AND amount_total > 0

# OR (explicit with |)
partners = self.env['res.partner'].search([
    '|',
    ('name', 'ilike', 'John'),
    ('email', 'ilike', 'john@'),
])
# SQL: WHERE name ILIKE 'John' OR email ILIKE 'john@'

# Complex OR with AND
records = self.env['sale.order'].search([
    '|',
    ('state', '=', 'sale'),
    ('state', '=', 'done'),
    ('amount_total', '>', 5000),
])
# SQL: WHERE (state = 'sale' OR state = 'done') AND amount_total > 5000

# NOT
inactive = self.env['res.partner'].search([
    '!',
    ('active', '=', True),
])
# SQL: WHERE NOT active = True

# Complex nesting
records = self.env['sale.order'].search([
    '|',
    '&',
        ('state', '=', 'sale'),
        ('amount_total', '>=', 1000),
    '&',
        ('state', '=', 'done'),
        ('date_order', '>=', '2024-01-01'),
])
# SQL: WHERE (state='sale' AND amount_total>=1000) 
#      OR (state='done' AND date_order>='2024-01-01')
```

### **3. JOINS (RELATIONS)**
```python
# Many2one join (automatic)
invoices = self.env['account.move'].search([
    ('partner_id.country_id.code', '=', 'PK'),  # Join via partner_id
])
# SQL: SELECT account_move.* FROM account_move
#      INNER JOIN res_partner ON account_move.partner_id = res_partner.id
#      INNER JOIN res_country ON res_partner.country_id = res_country.id
#      WHERE res_country.code = 'PK'

# One2many search (inverse relation)
partners_with_sales = self.env['res.partner'].search([
    ('sale_order_ids', '!=', False),  # Has at least one sale order
])

# Search within related records
customers = self.env['res.partner'].search([
    ('sale_order_ids.amount_total', '>', 100000),  # Has order > 100k
])
# Returns customers with at least one order over 100k

# Many2many search
users_with_role = self.env['res.users'].search([
    ('groups_id.name', '=', 'Sales / User'),  # In specific group
])

# Multiple joins
pickings = self.env['stock.picking'].search([
    ('partner_id.country_id.code', '=', 'US'),  # Partner country
    ('picking_type_id.warehouse_id.name', '=', 'Main'),  # Warehouse
    ('state', '=', 'done'),
])

# Join with compute fields (search in computed values)
products = self.env['product.product'].search([
    ('qty_available', '>', 100),  # Computed field
    ('categ_id.name', '=', 'Electronics'),
])

# LEFT JOIN equivalent (allow null)
# ORM doesn't have explicit LEFT JOIN, use:
partners = self.env['res.partner'].search([])  # All partners
no_contact = [p for p in partners if not p.email]  # Filter in Python
# Better approach: use SQL directly
self.env.cr.execute("""
    SELECT p.* FROM res_partner p
    LEFT JOIN account_move am ON am.partner_id = p.id
    WHERE am.id IS NULL  -- Partners with no invoices
""")
```

### **4. ORDERING & LIMITING**
```python
# Order ascending
orders = self.env['sale.order'].search(
    [('state', '!=', 'cancel')],
    order='date_order ASC'  # Default is ASC
)

# Order descending
recent_orders = self.env['sale.order'].search(
    [],
    order='date_order DESC'
)

# Multiple order fields
records = self.env['account.move'].search(
    [],
    order='partner_id ASC, date DESC'  # Partner name, then date
)

# Limit & offset
page_1 = self.env['sale.order'].search([], limit=20)  # First 20
page_2 = self.env['sale.order'].search([], offset=20, limit=20)  # Next 20

# Top N records
top_customers = self.env['sale.order'].search(
    [],
    order='amount_total DESC',
    limit=10  # Top 10 orders by value
)

# Get count without fetching
total_count = self.env['sale.order'].search_count([
    ('state', '!=', 'cancel')
])

# Search with order & count
sales_by_team = self.env['sale.order'].search(
    [],
    order='team_id ASC'
)
# Then group in Python or use GROUP BY in SQL
```

### **5. AGGREGATION & GROUPING**
```python
# SUM (via read_group)
result = self.env['sale.order'].read_group(
    [('state', '!=', 'cancel')],
    ['partner_id', 'amount_total:sum'],
    ['partner_id']
)
# Returns: [
#   {'partner_id': (1, 'ABC Corp'), 'amount_total': 500000, 'partner_id_count': 5},
#   {'partner_id': (2, 'XYZ Inc'), 'amount_total': 300000, 'partner_id_count': 3},
# ]

# COUNT
result = self.env['sale.order'].read_group(
    [],
    ['partner_id'],
    ['partner_id']
)
# Returns count of orders per partner

# AVG
result = self.env['account.move.line'].read_group(
    [('move_id.move_type', '=', 'out_invoice')],
    ['account_id', 'debit:avg', 'credit:avg'],
    ['account_id']
)

# Multiple aggregations
result = self.env['sale.order'].read_group(
    [('state', '=', 'sale')],
    [
        'partner_id',
        'amount_total:sum',
        'amount_tax:avg',
        'amount_untaxed:max',
    ],
    ['partner_id']
)

# With date grouping
result = self.env['sale.order'].read_group(
    [],
    ['date_order', 'amount_total:sum'],
    ['date_order:month']  # Group by month
)
# date_order:year, date_order:quarter, date_order:week also supported

# Nested grouping
result = self.env['sale.order'].read_group(
    [],
    ['partner_id', 'team_id', 'amount_total:sum'],
    ['partner_id', 'team_id']  # Group by partner, then by team
)

# Direct SQL aggregation
self.env.cr.execute("""
    SELECT partner_id, COUNT(*) as order_count, SUM(amount_total) as total
    FROM sale_order
    WHERE state != 'cancel'
    GROUP BY partner_id
    HAVING COUNT(*) > 5
    ORDER BY total DESC
""")
results = self.env.cr.dictfetchall()
# Returns: [
#   {'partner_id': 1, 'order_count': 8, 'total': 500000},
#   {'partner_id': 2, 'order_count': 6, 'total': 300000},
# ]
```

### **6. COMPLEX JOINS WITH MULTIPLE TABLES**
```python
# Customer → Sales Order → Invoice
invoices_from_vip = self.env['account.move'].search([
    ('move_type', '=', 'out_invoice'),
    ('invoice_origin', '!=', False),  # Linked to SO
    ('partner_id.sale_order_ids.amount_total', '>', 100000),  # Via SO
    ('date', '>=', '2024-01-01'),
])

# Multi-table join
pickings = self.env['stock.picking'].search([
    ('sale_id.partner_id.country_id.code', '=', 'US'),  # Customer country
    ('picking_type_id.code', '=', 'outgoing'),  # Delivery
    ('state', '=', 'done'),
    ('date_done', '>=', '2024-06-01'),
])

# Inventory movements with product info
moves = self.env['stock.move'].search([
    ('product_id.categ_id.name', '=', 'Electronics'),  # Product category
    ('location_id.warehouse_id.name', '=', 'Main'),  # From main warehouse
    ('location_dest_id.usage', '=', 'customer'),  # To customer
    ('state', '=', 'done'),
])

# GL entries with deep nesting
entries = self.env['account.move.line'].search([
    ('move_id.move_type', '=', 'out_invoice'),
    ('account_id.account_type', '=', 'asset'),
    ('move_id.partner_id.country_id.code', '!=', 'PK'),
    ('move_id.date', '>=', '2024-01-01'),
])

# SQL with multiple JOINs
self.env.cr.execute("""
    SELECT 
        so.id, so.name, so.amount_total,
        p.name as partner_name,
        ai.id as invoice_id,
        sp.id as picking_id
    FROM sale_order so
    INNER JOIN res_partner p ON so.partner_id = p.id
    LEFT JOIN account_move ai ON ai.invoice_origin = so.name
    LEFT JOIN stock_picking sp ON sp.sale_id = so.id
    WHERE so.state = 'sale'
    AND p.country_id = (SELECT id FROM res_country WHERE code = 'US')
    AND so.date_order >= '2024-01-01'
    ORDER BY so.amount_total DESC
    LIMIT 100
""")
results = self.env.cr.dictfetchall()
```

### **7. DISTINCT & UNIQUE VALUES**
```python
# Get unique customers who have sales
partners = self.env['sale.order'].search([]).mapped('partner_id')
# Automatically deduped, only unique partners returned

# Distinct via read_group
result = self.env['sale.order'].read_group(
    [],
    ['partner_id'],
    []  # No grouping = just distinct partners
)
# Returns list of unique partner_ids

# SQL DISTINCT
self.env.cr.execute("""
    SELECT DISTINCT country_id FROM res_partner
    WHERE active = True
    ORDER BY country_id
""")
countries = self.env.cr.fetchall()

# Unique with aggregation
result = self.env['account.move.line'].read_group(
    [('move_id.move_type', '=', 'out_invoice')],
    ['partner_id', '__count'],
    []  # Distinct partners only
)
```

### **8. SUBQUERIES & IN CLAUSES**
```python
# IN clause (ORM)
high_value_partners = self.env['res.partner'].search([
    ('sale_order_ids.amount_total', '>', 100000),
])

# Complex IN
invoices = self.env['account.move'].search([
    ('partner_id', 'in', high_value_partners.ids),  # IN clause
    ('state', '=', 'posted'),
])

# Subquery (SQL)
self.env.cr.execute("""
    SELECT * FROM account_move
    WHERE partner_id IN (
        SELECT DISTINCT partner_id FROM sale_order
        WHERE amount_total > 100000 AND YEAR(date_order) = 2024
    )
    AND state = 'posted'
    ORDER BY date DESC
""")
results = self.env.cr.dictfetchall()

# NOT IN
partners_no_sales = self.env['res.partner'].search([
    ('sale_order_ids', '=', False),  # No sales orders
])

# Complex subquery
self.env.cr.execute("""
    SELECT * FROM sale_order
    WHERE partner_id NOT IN (
        SELECT partner_id FROM account_move
        WHERE move_type = 'in_invoice' AND state = 'posted'
    )
    ORDER BY date_order DESC
""")
```

### **9. DATE & TIME QUERIES**
```python
# Today
today = self.env['sale.order'].search([
    ('date_order', '>=', fields.Date.today()),
    ('date_order', '<', fields.Date.add(fields.Date.today(), days=1)),
])

# This month
from datetime import datetime, timedelta
month_start = datetime(datetime.now().year, datetime.now().month, 1).date()
month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

invoices = self.env['account.move'].search([
    ('date', '>=', month_start),
    ('date', '<=', month_end),
])

# Last 30 days
thirty_days_ago = fields.Date.add(fields.Date.today(), days=-30)
recent = self.env['sale.order'].search([
    ('date_order', '>=', thirty_days_ago),
])

# Year
invoices_2024 = self.env['account.move'].search([
    ('date', '>=', '2024-01-01'),
    ('date', '<', '2025-01-01'),
])

# Between dates
records = self.env['sale.order'].search([
    ('date_order', '>=', '2024-01-01'),
    ('date_order', '<=', '2024-12-31'),
])

# SQL date operations
self.env.cr.execute("""
    SELECT * FROM sale_order
    WHERE YEAR(date_order) = 2024
    AND MONTH(date_order) IN (1, 2, 3)  -- Q1
    ORDER BY date_order
""")
```

### **10. PERFORMANCE OPTIMIZATION**
```python
# DON'T: N+1 queries
partners = self.env['res.partner'].search([])
for partner in partners:
    # This queries DB for each partner!
    orders = partner.sale_order_ids
    total = sum(o.amount_total for o in orders)

# DO: Batch load via computed field
# Better: Load once with read_group
result = self.env['sale.order'].read_group(
    [],
    ['partner_id', 'amount_total:sum'],
    ['partner_id']
)

# DON'T: Load all fields
records = self.env['sale.order'].search([])

# DO: Load specific fields
data = self.env['sale.order'].search([]).read(['name', 'amount_total'])

# DON'T: Create in loop
for vals in values_list:
    self.env['model'].create(vals)  # DB hit per record!

# DO: Batch create
self.env['model'].create(values_list)  # Single DB hit

# DON'T: Search + modify
records = self.env['account.move'].search([('state', '=', 'draft')])
for record in records:
    record.state = 'posted'  # Per-record update

# DO: Batch write
records.write({'state': 'posted'})  # Single update query

# DON'T: Expensive computed fields
@api.depends('line_ids')
def _compute_total(self):
    # If called in loop = N+1 computation
    self.total = sum(line.amount for line in self.line_ids)

# DO: Use read_group
result = self.env['sale.order.line'].read_group(
    [('order_id', 'in', order_ids)],
    ['order_id', 'price_total:sum'],
    ['order_id']
)

# Index for queries
# Add index on frequently searched fields:
# SELECT field_1, count(*)
# FROM table_1
# GROUP BY field_1
# WHERE field_1 is indexed, scan is fast
```

---

## 📊 **ADVANCED PATTERNS**

### **Window Functions (SQL)**
```python
# Ranking
self.env.cr.execute("""
    SELECT 
        id, name, amount_total,
        ROW_NUMBER() OVER (ORDER BY amount_total DESC) as rank,
        RANK() OVER (ORDER BY amount_total DESC) as rank_with_tie,
        DENSE_RANK() OVER (ORDER BY amount_total DESC) as dense_rank
    FROM sale_order
    WHERE state != 'cancel'
""")

# Running total
self.env.cr.execute("""
    SELECT 
        id, name, amount_total,
        SUM(amount_total) OVER (
            ORDER BY date_order 
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) as running_total
    FROM sale_order
    ORDER BY date_order
""")

# Partition with aggregation
self.env.cr.execute("""
    SELECT 
        id, partner_id, amount_total,
        AVG(amount_total) OVER (PARTITION BY partner_id) as avg_order_value,
        MAX(amount_total) OVER (PARTITION BY partner_id) as max_order,
        ROW_NUMBER() OVER (PARTITION BY partner_id ORDER BY amount_total DESC) as order_rank
    FROM sale_order
""")
```

### **Case/When (Conditional)**
```python
# SQL CASE
self.env.cr.execute("""
    SELECT 
        id, amount_total,
        CASE 
            WHEN amount_total >= 100000 THEN 'VIP'
            WHEN amount_total >= 50000 THEN 'Gold'
            WHEN amount_total >= 10000 THEN 'Silver'
            ELSE 'Standard'
        END as customer_tier
    FROM sale_order
    WHERE state != 'cancel'
    ORDER BY amount_total DESC
""")

# ORM computed equivalent
class SaleOrder(models.Model):
    
    @api.depends('amount_total')
    def _compute_tier(self):
        for order in self:
            if order.amount_total >= 100000:
                order.tier = 'VIP'
            elif order.amount_total >= 50000:
                order.tier = 'Gold'
            elif order.amount_total >= 10000:
                order.tier = 'Silver'
            else:
                order.tier = 'Standard'
    
    tier = fields.Char(compute='_compute_tier')
```

### **Recursive Queries (CTE)**
```python
# Category hierarchy
self.env.cr.execute("""
    WITH RECURSIVE category_tree AS (
        -- Base case: root categories
        SELECT id, name, parent_id, 0 as level
        FROM product_category
        WHERE parent_id IS NULL
        
        UNION ALL
        
        -- Recursive case: child categories
        SELECT pc.id, pc.name, pc.parent_id, ct.level + 1
        FROM product_category pc
        INNER JOIN category_tree ct ON pc.parent_id = ct.id
        WHERE ct.level < 10  -- Prevent infinite loops
    )
    SELECT * FROM category_tree
    WHERE level <= 3  -- Up to 3 levels deep
    ORDER BY level, name
""")
```

---

## 🎯 **ODOO-SPECIFIC QUERY PATTERNS**

### **Search Domain Syntax**
```python
# Format: [field_name, operator, value]
# Multiple conditions = AND (default)

# Operator examples
[('name', '=', 'ABC Corp')]          # Exact
[('name', '!=', 'ABC Corp')]         # Not equal
[('name', 'like', '%ABC%')]          # Contains
[('name', 'ilike', '%abc%')]         # Case-insensitive
[('name', '=like', 'ABC%')]          # Starts with
[('active', '=', True)]              # Boolean
[('id', 'in', [1, 2, 3])]            # In list
[('id', 'not in', [1, 2, 3])]        # Not in list
[('date', '>=', '2024-01-01')]       # Date
[('amount', '>', 1000)]              # Numeric
[('partner_id', '=', False)]         # Empty relation
[('tag_ids', '!=', False)]           # Has any tag
[('tag_ids', '=', 1)]                # Has specific tag
```

### **Read Group Aggregations**
```python
# Format: fields, groupby, orderby
result = self.env['model'].read_group(
    [('condition', '=', 'value')],          # Domain
    ['field1:sum', 'field2:avg', 'field3'],  # Aggregation fields
    ['groupby_field'],                       # Group by
    orderby='field1:sum DESC',               # Order
    lazy=False                                # Fetch all data
)

# Aggregation operators
# :sum, :avg, :min, :max, :count, :bool_and, :bool_or, :array_agg
```

---

## 📚 **COMMON QUERIES BY MODULE**

### **Sales Module**
```python
# Top 10 customers by revenue
result = self.env['sale.order'].read_group(
    [('state', '!=', 'cancel')],
    ['partner_id', 'amount_total:sum'],
    ['partner_id'],
    limit=10,
    orderby='__count:sum DESC'
)

# Orders this month
import datetime
today = datetime.date.today()
month_start = today.replace(day=1)
next_month = (month_start + datetime.timedelta(days=32)).replace(day=1)

orders = self.env['sale.order'].search([
    ('date_order', '>=', month_start),
    ('date_order', '<', next_month),
    ('state', '!=', 'cancel'),
])

# By salesperson
result = self.env['sale.order'].read_group(
    [('state', '=', 'sale')],
    ['user_id', 'amount_total:sum', 'amount_untaxed:sum'],
    ['user_id']
)
```

### **Accounting Module**
```python
# AR aging (invoices unpaid)
invoices = self.env['account.move'].search([
    ('move_type', '=', 'out_invoice'),
    ('state', '=', 'posted'),
    ('amount_residual', '!=', 0),
])

# GL balance by account
result = self.env['account.move.line'].read_group(
    [('move_id.state', '=', 'posted')],
    ['account_id', 'debit:sum', 'credit:sum'],
    ['account_id']
)

# Tax summary
result = self.env['account.move.line'].read_group(
    [('tax_ids', '!=', False)],
    ['tax_ids', 'tax_line_id', 'balance:sum'],
    ['tax_ids']
)
```

### **Inventory Module**
```python
# Stock on hand by location
result = self.env['stock.quant'].read_group(
    [('quantity', '>', 0)],
    ['product_id', 'location_id', 'quantity:sum'],
    ['product_id', 'location_id']
)

# Slow-moving inventory
result = self.env['stock.move'].read_group(
    [('state', '=', 'done'), ('date', '<', '2023-01-01')],
    ['product_id', 'quantity_done:sum'],
    ['product_id'],
    orderby='quantity_done:sum DESC'
)
```

### **HR & Payroll Module**
```python
# Payroll summary
result = self.env['hr.payslip.line'].read_group(
    [('code', '=', 'NET')],
    ['employee_id', 'amount:sum'],
    ['employee_id']
)

# Attendance by employee
result = self.env['hr.attendance'].read_group(
    [('check_in', '>=', '2024-01-01')],
    ['employee_id', 'worked_hours:sum'],
    ['employee_id']
)
```

### **CRM Module**
```python
# Pipeline forecast
result = self.env['crm.lead'].read_group(
    [('type', '=', 'opportunity')],
    ['stage_id', 'expected_revenue:sum'],
    ['stage_id']
)

# Lead source ROI
result = self.env['crm.lead'].read_group(
    [],
    ['source_id', '__count'],
    ['source_id']
)
```

---

## ⚡ **PERFORMANCE TIPS**

```python
# 1. Limit fields
users = self.env['res.users'].search([]).read(['name', 'email'])
# Instead of: users = self.env['res.users'].search([])

# 2. Use search_count
count = self.env['sale.order'].search_count([('state', '!=', 'cancel')])
# Instead of: len(self.env['sale.order'].search([...]))

# 3. Batch operations
records.write({'stage_id': stage_id})  # One SQL update
# Instead of: for r in records: r.stage_id = stage_id

# 4. use read_group for aggregation
result = self.env['sale.order'].read_group(..., lazy=False)
# Instead of: computing in Python

# 5. SQL for complex queries
self.env.cr.execute("""SELECT ...""")
# Instead of: complex ORM chains
```

---

**Skill Status:** ✅ Ready for all query tasks

