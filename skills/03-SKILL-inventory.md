# Odoo Inventory & Warehouse Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE PATTERNS & WORKFLOWS

### **1. STOCK LOCATIONS & HIERARCHY**
```python
# Locations define inventory structure
# Physical locations for goods

class StockLocation(models.Model):
    _name = 'stock.location'
    
    name = fields.Char(required=True)
    location_id = fields.Many2one('stock.location', 'Parent')  # Hierarchy
    
    # Location type
    usage = fields.Selection([
        ('supplier', 'Supplier location'),
        ('view', 'View (no actual stock)'),
        ('internal', 'Internal location'),
        ('customer', 'Customer location'),
        ('inventory', 'Inventory adjustment'),
        ('production', 'Production location'),
        ('transit', 'Transit location')
    ])
    
    # Scrap/Returns locations
    scrap_location = fields.Boolean()
    return_location = fields.Boolean()
    
    # Warehouse config
    warehouse_id = fields.Many2one('stock.warehouse')
    
    active = fields.Boolean(default=True)

# Typical hierarchy:
# └─ Company
#    ├─ Warehouse
#    │  ├─ Stock (internal - WH-STOCK)
#    │  ├─ Quality Control (internal - WH-QC)
#    │  ├─ Production (internal - WH-PROD)
#    │  └─ Scrap (scrap)
#    ├─ Supplier Locations (virtual - for each vendor)
#    ├─ Customer Locations (virtual - for each customer)
#    └─ Transit (for inter-warehouse transfers)

# Stock Quant = Physical inventory unit
class StockQuant(models.Model):
    _name = 'stock.quant'
    
    product_id = fields.Many2one('product.product', required=True)
    location_id = fields.Many2one('stock.location', required=True)
    quantity = fields.Float()  # On-hand qty
    
    # Reservation for SO/MO
    reserved_quantity = fields.Float()
    available_quantity = fields.Float(
        compute='_compute_available'
    )  # = quantity - reserved_quantity
    
    # Tracking
    lot_id = fields.Many2one('stock.production.lot')  # Batch/Serial
    package_id = fields.Many2one('stock.quant.package')  # Pallet/Container
    
    owner_id = fields.Many2one('res.partner')  # Consignment stock
    
    def _compute_available(self):
        self.available_quantity = self.quantity - self.reserved_quantity
```

### **2. STOCK MOVES (CORE FLOW)**
```python
class StockMove(models.Model):
    _name = 'stock.move'
    
    name = fields.Char(auto=True)
    
    # What
    product_id = fields.Many2one('product.product', required=True)
    product_qty = fields.Float(required=True)
    product_uom = fields.Many2one('uom.uom')
    
    # From → To
    location_id = fields.Many2one('stock.location', required=True)  # Source
    location_dest_id = fields.Many2one('stock.location', required=True)  # Dest
    
    # Status
    state = fields.Selection([
        ('draft', 'Draft'),
        ('waiting', 'Waiting'),
        ('confirmed', 'Confirmed'),
        ('assigned', 'Assigned'),
        ('done', 'Done'),
        ('cancel', 'Cancelled')
    ])
    
    # Linked document
    origin = fields.Char()  # Reference (PO/SO number)
    picking_id = fields.Many2one('stock.picking')  # Group moves
    
    # Tracking
    lot_ids = fields.Many2many('stock.production.lot')
    package_id = fields.Many2one('stock.quant.package')
    
    # For backorders (partial delivery)
    move_line_ids = fields.One2many('stock.move.line', 'move_id')
    
    def _action_confirm(self):
        """Confirm move - reserve stock if available"""
        self.state = 'confirmed'
        
        # Trigger: Check availability
        self._action_assign()

    def _action_assign(self):
        """
        Try to assign move to available stock quants
        state: confirmed → assigned (or waiting if no stock)
        """
        available_quants = self.env['stock.quant'].search([
            ('product_id', '=', self.product_id.id),
            ('location_id', '=', self.location_id.id),
            ('available_quantity', '>=', self.product_qty),
        ])
        
        if available_quants:
            # Reserve quants
            quant = available_quants[0]
            quant.reserved_quantity += self.product_qty
            
            # Create move line (actual fulfillment)
            self.env['stock.move.line'].create({
                'move_id': self.id,
                'product_id': self.product_id.id,
                'qty_done': self.product_qty,
                'lot_id': quant.lot_id.id,
                'location_id': self.location_id.id,
                'location_dest_id': self.location_dest_id.id,
            })
            
            self.state = 'assigned'
        else:
            self.state = 'waiting'  # Backorder

    def _action_done(self):
        """Execute move - update quants"""
        # Move quants from source → dest
        source_quant = self.env['stock.quant'].search([
            ('product_id', '=', self.product_id.id),
            ('location_id', '=', self.location_id.id),
        ])
        
        dest_quant = self.env['stock.quant'].search([
            ('product_id', '=', self.product_id.id),
            ('location_id', '=', self.location_dest_id.id),
        ])
        
        # Subtract from source
        if source_quant:
            source_quant.quantity -= self.product_qty
            source_quant.reserved_quantity -= self.product_qty
        
        # Add to destination
        if dest_quant:
            dest_quant.quantity += self.product_qty
        else:
            # Create new quant if doesn't exist
            self.env['stock.quant'].create({
                'product_id': self.product_id.id,
                'location_id': self.location_dest_id.id,
                'quantity': self.product_qty,
            })
        
        self.state = 'done'
        
        # Create GL entry (if internal valuation)
        self._create_account_entry()

class StockMoveLine(models.Model):
    _name = 'stock.move.line'
    
    move_id = fields.Many2one('stock.move')
    product_id = fields.Many2one('product.product')
    
    # What was actually moved
    qty_done = fields.Float()
    
    # Where
    location_id = fields.Many2one('stock.location')
    location_dest_id = fields.Many2one('stock.location')
    
    # Tracking details
    lot_id = fields.Many2one('stock.production.lot')  # Batch
    serial_number = fields.Char()  # Individual serial
    package_id = fields.Many2one('stock.quant.package')
```

### **3. PICKINGS (SHIPMENTS)**
```python
class StockPicking(models.Model):
    _name = 'stock.picking'
    
    name = fields.Char(auto=True)  # Pick-001
    
    # Type of operation
    picking_type_id = fields.Many2one('stock.picking.type')
    # Predefined: Receipts, Delivery Orders, Internal Transfers
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('waiting', 'Waiting Another Operation'),
        ('confirmed', 'Confirmed'),
        ('assigned', 'Ready'),
        ('done', 'Done'),
        ('cancel', 'Cancelled')
    ])
    
    # Source document
    origin = fields.Char()  # PO/SO reference
    
    # Grouped moves
    move_ids = fields.One2many('stock.move', 'picking_id')
    move_line_ids = fields.One2many('stock.move.line', 'picking_id')
    
    # Partner (if receipt/delivery)
    partner_id = fields.Many2one('res.partner')
    
    # Locations
    location_id = fields.Many2one('stock.location')  # Source
    location_dest_id = fields.Many2one('stock.location')  # Dest
    
    user_id = fields.Many2one('res.users')  # Assigned to

class StockPickingType(models.Model):
    _name = 'stock.picking.type'
    
    name = fields.Char(required=True)  # 'Receipts', 'Delivery Orders'
    code = fields.Selection([
        ('incoming', 'Incoming'),
        ('outgoing', 'Outgoing'),
        ('internal', 'Internal'),
    ])
    
    # Locations configured
    default_location_src_id = fields.Many2one('stock.location')  # Default from
    default_location_dest_id = fields.Many2one('stock.location')  # Default to
    
    # Workflow: 1-step, 2-step, or 3-step?
    sequence_id = fields.Many2one('ir.sequence')
    
    # Associated warehouse
    warehouse_id = fields.Many2one('stock.warehouse')

# PICKING WORKFLOW
def action_confirm(self):
    """Confirm all moves in picking"""
    for move in self.move_ids:
        move._action_confirm()
    self.state = 'confirmed'

def action_assign(self):
    """Try to reserve stock for all moves"""
    for move in self.move_ids:
        move._action_assign()
    
    # If all assigned → ready, else → waiting
    if all(m.state == 'assigned' for m in self.move_ids):
        self.state = 'assigned'
    else:
        self.state = 'waiting'

def button_validate(self):
    """Execute all moves (in warehouse app)"""
    # User scans/confirms items picked
    # Move lines get qty_done filled
    for move in self.move_ids:
        move._action_done()
    
    self.state = 'done'
```

### **4. WAREHOUSE CONFIGURATION & WORKFLOWS**
```python
class StockWarehouse(models.Model):
    _name = 'stock.warehouse'
    
    name = fields.Char(required=True)  # 'Main Warehouse'
    company_id = fields.Many2one('res.company')
    
    # Locations
    lot_stock_id = fields.Many2one('stock.location')  # Stock location
    wh_qc_stock_id = fields.Many2one('stock.location')  # QC area
    
    # Operation steps
    in_type_id = fields.Many2one('stock.picking.type')  # Receipt type
    out_type_id = fields.Many2one('stock.picking.type')  # Delivery type
    int_type_id = fields.Many2one('stock.picking.type')  # Transfer type
    
    # Manufacturing operations
    manufacture_steps = fields.Selection([
        ('mrp_one_step', '1-Step: Raw → Finished'),
        ('mrp_two_step', '2-Step: Raw → Production → Finished'),
    ])

"""
WAREHOUSE OPERATION STEPS:

1-STEP FLOW (Simple):
  Supplier → Stock
  Stock → Customer
  (No intermediate queues)

2-STEP FLOW (Most Common):
  Supplier → Receiving → Stock
  Stock → Picking → Customer
  (Allows inspection between steps)

3-STEP FLOW (Complex):
  Supplier → Receiving → QC → Stock
  Stock → Picking → Packing → Customer
  (Full granular control)
"""

# Configuration in warehouse:
# Receiving: 2 steps? → Creates REC + RECEIPT picking types
# Delivery: 2 steps? → Creates PICK + DELIVERY picking types

class StockRoute(models.Model):
    """Pull/Push rules for automatic movements"""
    _name = 'stock.route'
    
    name = fields.Char()  # 'Manufacture on Demand'
    
    # When to apply
    product_categ_ids = fields.Many2many('product.category')
    
    rule_ids = fields.One2many('stock.rule', 'route_id')

class StockRule(models.Model):
    """Auto-create moves based on demand"""
    _name = 'stock.rule'
    
    route_id = fields.Many2one('stock.route')
    
    # Trigger
    location_from_id = fields.Many2one('stock.location')
    location_dest_id = fields.Many2one('stock.location')
    
    # Action
    action = fields.Selection([
        ('pull', 'Pull'),  # Fetch from supplier
        ('push', 'Push'),  # Send to next location
    ])
    
    # Rule types
    procure_method = fields.Selection([
        ('make_to_stock', 'Make To Stock'),
        ('make_to_order', 'Make To Order'),
    ])

# Example route: 'Manufacture on Demand'
# Rule: If SO line → Customer location
# Then create MO for that product
# Then move from Production → Customer
```

### **5. MULTI-STEP INBOUND FLOW (2-STEP)**
```python
"""
Supplier sends goods → Receiving → QC → Stock

Step 1: Receiving (GRN/receipt confirmation)
Step 2: Moving to stock (after inspection)
"""

# PO received → Auto-create TWO pickings:

# PICKING 1: Receipt from supplier
receipt = self.env['stock.picking'].create({
    'picking_type_id': warehouse.in_type_id.id,  # Receipt
    'partner_id': po.partner_id.id,
    'origin': po.name,
    'move_ids': [
        (0, 0, {
            'product_id': line.product_id.id,
            'product_qty': line.product_qty,
            'location_id': SUPPLIER_LOCATION.id,
            'location_dest_id': RECEIVING_AREA.id,  # Temp location
        })
        for line in po.order_line
    ]
})

# PICKING 2: Move from receiving to stock
put_away = self.env['stock.picking'].create({
    'picking_type_id': warehouse.int_type_id.id,  # Internal transfer
    'move_ids': [
        (0, 0, {
            'product_id': line.product_id.id,
            'product_qty': line.product_qty,
            'location_id': RECEIVING_AREA.id,
            'location_dest_id': warehouse.lot_stock_id.id,  # Stock bin
        })
        for line in po.order_line
    ]
})

# User workflow:
# 1. Warehouse receives package → Scan receipt picking
# 2. Items go to receiving temp location
# 3. QC checks → Creates put-away picking
# 4. Warehouse staff scans put-away → Items go to bins
```

### **6. MULTI-STEP OUTBOUND FLOW (2-STEP)**
```python
"""
Stock → Picking → Packing → Customer

Step 1: Picking (pull from bins)
Step 2: Packing (prepare shipment)
"""

# SO confirmed → Auto-create TWO pickings:

# PICKING 1: Pick from stock
picking = self.env['stock.picking'].create({
    'picking_type_id': warehouse.out_type_id.id,  # Pick
    'partner_id': so.partner_id.id,
    'origin': so.name,
    'move_ids': [
        (0, 0, {
            'product_id': line.product_id.id,
            'product_qty': line.product_qty,
            'location_id': warehouse.lot_stock_id.id,  # Stock
            'location_dest_id': PACKING_AREA.id,  # Temp
        })
        for line in so.order_line
    ]
})

# PICKING 2: Move to shipping
delivery = self.env['stock.picking'].create({
    'picking_type_id': warehouse.out_type_id.id,  # Delivery
    'move_ids': [
        (0, 0, {
            'product_id': line.product_id.id,
            'product_qty': line.product_qty,
            'location_id': PACKING_AREA.id,
            'location_dest_id': CUSTOMER_LOCATION.id,
        })
        for line in so.order_line
    ]
})

# User workflow:
# 1. Warehouse picks items → Scan picking (items → packing area)
# 2. Packing team packs → Scans delivery (items leave warehouse)
# 3. Carrier takes shipment
```

### **7. RESERVATIONS & BACKORDERS**
```python
"""
Reservation: When SO confirmed, reserve qty from stock
Backorder: If not enough stock, create backorder for rest
"""

class SaleOrder(models.Model):
    
    def action_confirm(self):
        # For each line → Create stock.move
        for line in self.order_line:
            move = self.env['stock.move'].create({
                'product_id': line.product_id.id,
                'product_qty': line.product_uom_qty,
                'location_id': warehouse.lot_stock_id.id,
                'location_dest_id': CUSTOMER_LOCATION.id,
                'picking_type_id': warehouse.out_type_id.id,
            })
            move._action_confirm()  # Auto-reserve if available
            
            # If backorder:
            if move.state == 'waiting':
                # Mark SO as waiting for stock
                line.state = 'backorder'

# Backorder picking - separate picking for partial qty
# Can be fulfilled later when stock arrives
```

### **8. LOT & SERIAL NUMBER TRACKING**
```python
class StockProductionLot(models.Model):
    _name = 'stock.production.lot'
    
    name = fields.Char(required=True)  # Batch/Lot number
    product_id = fields.Many2one('product.product', required=True)
    
    # Batch info
    expiration_date = fields.Date()
    
    # Traceability
    create_date = fields.Datetime()  # Manufacturing date
    
    tracking = fields.Selection([
        ('serial', 'By Serial Number'),
        ('lot', 'By Lot'),
        ('none', 'None')
    ])

# Tracking in move line
class StockMoveLine(models.Model):
    
    lot_id = fields.Many2one('stock.production.lot')  # Batch
    serial_number = fields.Char()  # Individual serial
    
    # For serial-tracked products:
    # Each unit gets unique serial
    # E.g., Laptop serial: ABC123, ABC124, ABC125

# Expiration date logic:
# If lot has expiry_date, and TODAY > expiry_date
# → Alert in picking (expired stock)
# → Cannot be used for picking
```

### **9. INVENTORY VALUATIONS & COSTING**
```python
class StockMove(models.Model):
    
    def _create_account_entry(self):
        """
        When move done: GL entry for inventory
        Debit: Stock account
        Credit: Expense / Supplier
        """
        
        # Get product category accounts
        product_categ = self.product_id.categ_id
        
        if self.location_id.usage == 'supplier':
            # Receiving from supplier
            account_debit = product_categ.property_stock_account_input_categ_id
            account_credit = product_categ.property_account_expense_categ_id
            
        elif self.location_dest_id.usage == 'customer':
            # Shipping to customer (COGS)
            account_debit = product_categ.property_account_expense_categ_id
            account_credit = product_categ.property_stock_account_output_categ_id
        
        # Cost calculation
        cost_unit = self.product_id.standard_price
        total_cost = cost_unit * self.product_qty
        
        # Create GL entry
        self.env['account.move'].create({
            'move_type': 'entry',
            'line_ids': [
                (0, 0, {
                    'account_id': account_debit.id,
                    'debit': total_cost,
                    'name': f"Stock in: {self.product_id.name}",
                }),
                (0, 0, {
                    'account_id': account_credit.id,
                    'credit': total_cost,
                })
            ]
        }).action_post()

# Costing methods (per product):
class ProductProduct(models.Model):
    
    cost_method = fields.Selection([
        ('standard', 'Standard Cost - Fixed'),
        ('fifo', 'FIFO - First In First Out'),
        ('average', 'Average Cost - Running average'),
    ])
```

### **10. CYCLE COUNTING & INVENTORY ADJUSTMENT**
```python
class StockInventory(models.Model):
    _name = 'stock.inventory'
    
    name = fields.Char(auto=True)
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirm', 'Confirmed'),
        ('done', 'Validated'),
    ])
    
    # Which locations to count
    location_ids = fields.Many2many('stock.location')
    
    # Counted quantities
    line_ids = fields.One2many('stock.inventory.line', 'inventory_id')

class StockInventoryLine(models.Model):
    _name = 'stock.inventory.line'
    
    inventory_id = fields.Many2one('stock.inventory')
    product_id = fields.Many2one('product.product')
    
    # System qty (what Odoo thinks we have)
    product_qty = fields.Float(related='stock_quant_id.quantity')
    
    # Actual qty (what was physically counted)
    inventory_quantity = fields.Float()
    
    # Difference
    difference_qty = fields.Float(
        compute='_compute_difference'
    )  # inventory_quantity - product_qty

# Workflow:
def action_start(self):
    """
    Print count sheet
    Warehouse staff counts physical inventory
    """
    pass

def action_validate(self):
    """
    For each line:
    If system != actual:
    - Create adjustment stock move
    - Qty difference → scrap or gain
    """
    for line in self.line_ids:
        if line.difference_qty != 0:
            # Create stock move to adjust
            self.env['stock.move'].create({
                'product_id': line.product_id.id,
                'product_qty': abs(line.difference_qty),
                'location_id': line.inventory_id.location_ids[0].id,
                'location_dest_id': (SCRAP_LOCATION.id 
                                    if line.difference_qty < 0 
                                    else GAIN_LOCATION.id),
            })._action_done()
```

---

## 📚 **KEY MODELS CHEAT SHEET**

| Model | Purpose |
|-------|---------|
| **stock.location** | Physical/virtual locations |
| **stock.quant** | On-hand inventory |
| **stock.move** | Movement record |
| **stock.picking** | Shipment/receipt |
| **stock.warehouse** | Warehouse config |
| **stock.production.lot** | Batch/lot tracking |
| **stock.inventory** | Physical count |

---

## 🔧 **COMMON TASKS**

### **Check Product Availability**
```python
product = self.env['product.product'].browse(product_id)
quant = self.env['stock.quant'].search([
    ('product_id', '=', product.id),
    ('location_id', '=', warehouse.lot_stock_id.id)
])
available = quant.available_quantity if quant else 0
```

### **Create Internal Transfer**
```python
self.env['stock.picking'].create({
    'picking_type_id': warehouse.int_type_id.id,
    'move_ids': [(0, 0, {
        'product_id': product_id,
        'product_qty': 10,
        'location_id': FROM_LOCATION.id,
        'location_dest_id': TO_LOCATION.id,
    })]
}).action_confirm()
```

### **Adjust Inventory (Cycle Count)**
```python
inv = self.env['stock.inventory'].create({
    'location_ids': [(6, 0, [location_id])],
    'line_ids': [(0, 0, {
        'product_id': product_id,
        'inventory_quantity': 50,  # Actual count
    })]
})
inv.action_validate()  # Creates adjustment move
```

---

## ⚠️ **CRITICAL RULES**

1. **Moves are ordered**: Confirmed → Assigned → Done
2. **Reservations lock qty**: Cannot modify reserved quantities
3. **Balance inventory**: Moves must have source & dest
4. **Backorders allowed**: Partial delivery creates backorder move
5. **Tracking mandatory**: Tracked products require lot/serial

---

## 🚀 **QUICK REFERENCE**

- **Default route**: Make-to-stock (consume from warehouse stock)
- **2-step default**: Most warehouses use 2-step flows for visibility
- **Picking = move**: A picking is a container for 1+ moves
- **Quants lock qty**: Reserved_quantity ≤ quantity
- **Scrap location**: For counted/damaged inventory

---

**Skill Status:** ✅ Ready for all Inventory & Warehouse tasks
