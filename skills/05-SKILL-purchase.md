# Odoo Purchase & Procurement Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE PATTERNS & WORKFLOWS

### **1. PURCHASE ORDER LIFECYCLE**
```python
# Workflow: RFQ → PO → Receipt (GRN) → Bill → Payment

class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    
    name = fields.Char(auto=True)  # PO-2024-001
    
    state = fields.Selection([
        ('draft', 'Draft RFQ'),
        ('sent', 'RFQ Sent'),
        ('to approve', 'To Approve'),
        ('purchase', 'Purchase Order'),
        ('done', 'Locked'),
        ('cancel', 'Cancelled')
    ])
    
    # Vendor
    partner_id = fields.Many2one('res.partner', required=True)
    
    # Lines
    order_line = fields.One2many('purchase.order.line', 'order_id')
    
    # Amount tracking
    amount_untaxed = fields.Float(compute='_compute_amounts')
    amount_tax = fields.Float(compute='_compute_amounts')
    amount_total = fields.Float(compute='_compute_amounts')
    
    # Linked documents
    picking_ids = fields.One2many('stock.picking', 'purchase_id')
    invoice_ids = fields.One2many('account.move', 'purchase_id')
    
    # Dates
    date_order = fields.Datetime(default=fields.Datetime.now)
    date_planned = fields.Datetime()  # Expected delivery
    
    # Approval
    user_id = fields.Many2one('res.users')  # Buyer
    approve_status = fields.Selection([
        ('waiting_approval', 'Waiting Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected')
    ])

# LIFECYCLE TRANSITIONS

def action_rfq_send(self):
    """Send RFQ to vendor"""
    # Create vendor quotation
    # Send email with terms
    self.state = 'sent'

def action_rfq_confirm(self):
    """Convert RFQ → Purchase Order"""
    # Prices locked
    # Stock moves created
    # Vendor notified
    self.state = 'purchase'
    
    # Create stock moves for receipt
    for line in self.order_line:
        self.env['stock.picking'].create({
            'picking_type_id': warehouse.in_type_id.id,
            'purchase_id': self.id,
            'partner_id': self.partner_id.id,
            'move_ids': [(0, 0, {
                'product_id': line.product_id.id,
                'product_qty': line.product_qty,
            })]
        })

def action_cancel(self):
    """Cancel PO"""
    # Reverse stock moves if any
    # Notify vendor
    self.state = 'cancel'

def button_approve(self):
    """Approval workflow"""
    # If PO amount > approval limit, needs manager approval
    if self.amount_total > APPROVAL_LIMIT:
        self.approve_status = 'waiting_approval'
    else:
        self.approve_status = 'approved'
```

### **2. PURCHASE ORDER LINES & PRICING**
```python
class PurchaseOrderLine(models.Model):
    _name = 'purchase.order.line'
    
    order_id = fields.Many2one('purchase.order', required=True)
    
    # Product & quantity
    product_id = fields.Many2one('product.product', required=True)
    product_qty = fields.Float(required=True)  # Order qty
    product_uom = fields.Many2one('uom.uom')
    
    # Pricing
    price_unit = fields.Float()  # Unit price from vendor
    
    # Calculated
    price_subtotal = fields.Float(compute='_compute_amount_line')
    price_tax = fields.Float(compute='_compute_amount_line')
    price_total = fields.Float(compute='_compute_amount_line')
    
    # Tax
    tax_id = fields.Many2many('account.tax')
    
    # Scheduling
    schedule_date = fields.Datetime()  # When vendor will deliver
    
    # Receipt tracking
    qty_received = fields.Float(compute='_compute_qty_received')  # From GRN
    qty_billed = fields.Float(compute='_compute_qty_billed')  # From invoice
    qty_to_bill = fields.Float(compute='_compute_qty_to_bill')
    
    # 3-way match tracking
    qty_to_invoice = fields.Float()  # Qty vendor invoiced
    
    # Lead time info
    date_planned = fields.Datetime()  # Vendor's delivery date
    
    @api.depends('product_qty', 'price_unit', 'tax_id')
    def _compute_amount_line(self):
        """Calculate line totals"""
        for line in self:
            subtotal = line.price_unit * line.product_qty
            
            tax_amount = 0
            for tax in line.tax_id:
                if tax.amount_type == 'percent':
                    tax_amount += subtotal * (tax.amount / 100)
            
            line.price_subtotal = subtotal
            line.price_tax = tax_amount
            line.price_total = subtotal + tax_amount
```

### **3. REQUEST FOR QUOTATION (RFQ)**
```python
class PurchaseRequisition(models.Model):
    _name = 'purchase.requisition'
    
    name = fields.Char(auto=True)
    
    # RFQ type
    type_id = fields.Many2one('purchase.requisition.type')  # Standard, Tender
    
    # Lines to quote
    line_ids = fields.One2many('purchase.requisition.line', 'requisition_id')
    
    # Multiple vendors
    vendor_ids = fields.Many2many('res.partner')
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('ongoing', 'Sent to Vendors'),
        ('in_progress', 'Waiting Responses'),
        ('done', 'Closed'),
    ])
    
    # Deadline
    date_end = fields.Datetime()  # RFQ deadline

class PurchaseRequisitionLine(models.Model):
    _name = 'purchase.requisition.line'
    
    requisition_id = fields.Many2one('purchase.requisition')
    product_id = fields.Many2one('product.product')
    product_qty = fields.Float()

# VENDOR COMPARISON
class PurchaseRequisitionVendorLine(models.Model):
    _name = 'purchase.requisition.vendor.line'
    
    requisition_id = fields.Many2one('purchase.requisition')
    vendor_id = fields.Many2one('res.partner')  # Vendor responded
    
    # Vendor's quote
    price_unit = fields.Float()
    delivery_date = fields.Date()  # Vendor delivery promise
    
    # Evaluation
    rating = fields.Float()  # Score: 0-10
    comment = fields.Text()
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected')
    ])
    
    def action_accept(self):
        """Create PO from this vendor quote"""
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor_id.id,
            'order_line': [
                (0, 0, {
                    'product_id': line.product_id.id,
                    'product_qty': line.product_qty,
                    'price_unit': self.price_unit,
                })
                for line in self.requisition_id.line_ids
            ]
        })
        return po
```

### **4. 3-WAY MATCH: PO → GRN → BILL**
```python
"""
3-Way Match validates:
1. Purchase Order (qty & price)
2. Goods Receipt Note (qty received)
3. Vendor Bill (qty & price invoiced)

All three must match within tolerance (~2%)
"""

class PurchaseOrder(models.Model):
    
    def create_bill_from_po(self, po_lines):
        """
        Create vendor bill from PO
        Auto-fill quantities & prices from PO
        """
        invoice_lines = []
        for line in po_lines:
            invoice_lines.append((0, 0, {
                'product_id': line.product_id.id,
                'quantity': line.product_qty,  # Default to PO qty
                'price_unit': line.price_unit,
                'purchase_line_id': line.id,
            }))
        
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'purchase_id': self.id,
            'partner_id': self.partner_id.id,
            'line_ids': invoice_lines,
        })
        
        return bill
    
    def validate_3way_match(self):
        """
        Check PO → GRN → Bill alignment
        Returns: {
            'po_qty': 100,
            'grn_qty': 98,
            'bill_qty': 100,
            'variance': OK/WARNING/ERROR,
            'messages': ['GRN qty 2 short of PO', ...]
        }
        """
        messages = []
        variance = 'OK'
        
        for po_line in self.order_line:
            # PO quantity
            po_qty = po_line.product_qty
            
            # GRN quantity (from picking)
            grn_qty = po_line.qty_received
            
            # Bill quantity (from invoice)
            bill_qty = po_line.qty_billed
            
            # Check tolerance (2%)
            tolerance = po_qty * 0.02
            
            # 1. PO vs GRN
            if abs(grn_qty - po_qty) > tolerance:
                messages.append(
                    f"{po_line.product_id.name}: "
                    f"GRN qty {grn_qty} vs PO qty {po_qty}"
                )
                variance = 'WARNING'
            
            # 2. PO vs Bill
            if abs(bill_qty - po_qty) > tolerance:
                messages.append(
                    f"{po_line.product_id.name}: "
                    f"Bill qty {bill_qty} vs PO qty {po_qty}"
                )
                variance = 'WARNING'
            
            # 3. GRN vs Bill (qty received must be invoiced)
            if bill_qty > grn_qty:
                messages.append(
                    f"{po_line.product_id.name}: "
                    f"Bill qty {bill_qty} exceeds GRN qty {grn_qty}"
                )
                variance = 'ERROR'
        
        return {
            'variance': variance,
            'messages': messages,
        }
```

### **5. VENDOR MANAGEMENT & TERMS**
```python
class ResPartner(models.Model):
    
    # Vendor info
    is_company = fields.Boolean()
    supplier_rank = fields.Integer()  # Priority vendor (1=primary)
    
    # Payment terms
    payment_term_id = fields.Many2one('account.payment.term')
    
    # Lead time
    supplier_delay = fields.Integer()  # Days to deliver
    
    # Pricing
    property_purchase_currency_id = fields.Many2one('res.currency')
    
    # Quality rating
    rating = fields.Float()  # 0-10 score
    
    # Purchase history
    purchase_order_count = fields.Integer(compute='_compute_order_count')
    total_spend = fields.Float(compute='_compute_total_spend')
    
    def get_supplier_price(self, product, qty):
        """Get preferred supplier's price"""
        supplier_info = self.env['product.supplierinfo'].search([
            ('partner_id', '=', self.id),
            ('product_id', '=', product.id),
            ('min_qty', '<=', qty),
        ], order='min_qty desc', limit=1)
        
        if supplier_info:
            return supplier_info.price
        
        return product.standard_price

class ProductSupplierinfo(models.Model):
    _name = 'product.supplierinfo'
    
    partner_id = fields.Many2one('res.partner', required=True)
    product_id = fields.Many2one('product.product')
    product_tmpl_id = fields.Many2one('product.template')
    
    # Tiered pricing
    min_qty = fields.Float()  # Min qty to get this price
    price = fields.Float()  # Unit price
    
    # Lead time
    delay = fields.Integer()  # Days to deliver
    
    # Active
    active = fields.Boolean(default=True)
    
    # Preferred/backup
    is_preferred = fields.Boolean()

# VENDOR SCORECARD
class VendorScorecard(models.Model):
    _name = 'vendor.scorecard'
    
    partner_id = fields.Many2one('res.partner')
    date_from = fields.Date()
    date_to = fields.Date()
    
    # Metrics
    on_time_delivery = fields.Float()  # %
    quality_rating = fields.Float()  # 0-100
    response_time = fields.Float()  # Hours
    invoice_accuracy = fields.Float()  # % correct
    
    # Overall
    overall_score = fields.Float(compute='_compute_overall')
    
    def _compute_overall(self):
        """Weighted average of metrics"""
        self.overall_score = (
            (self.on_time_delivery * 0.4) +
            (self.quality_rating * 0.3) +
            (self.response_time * 0.2) +
            (self.invoice_accuracy * 0.1)
        )
```

### **6. BLANKET ORDERS & PURCHASE AGREEMENTS**
```python
class PurchaseOrderType(models.Model):
    _name = 'purchase.order.type'
    
    name = fields.Char()  # 'Standard', 'Blanket', 'Contract'
    code = fields.Char()

class PurchaseOrder(models.Model):
    
    # Type
    type_id = fields.Many2one('purchase.order.type')
    
    # For blanket orders
    is_blanket = fields.Boolean()
    
    # Releases from blanket (stored separately)
    release_ids = fields.One2many('purchase.order.release', 'blanket_order_id')

class PurchaseOrderRelease(models.Model):
    _name = 'purchase.order.release'
    
    blanket_order_id = fields.Many2one('purchase.order')
    
    # Specific release (mini-PO)
    release_qty = fields.Float()
    release_date = fields.Date()
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('released', 'Released'),
        ('received', 'Received'),
        ('billed', 'Billed')
    ])

# BLANKET ORDER USAGE:
# 1. Create blanket PO with vendor for 1000 units @ $10
# 2. Release 100 units on Monday (GRN)
# 3. Release 150 units on Wednesday (GRN)
# 4. Remaining 750 units available (no GRN yet)
# Vendor bills per release received
```

### **7. PURCHASE REQUISITIONS (INTERNAL)**
```python
class PurchaseRequisition(models.Model):
    _name = 'purchase.requisition'
    
    # Internal request (from department)
    requested_by = fields.Many2one('res.users')
    department_id = fields.Many2one('hr.department')
    
    # Business justification
    description = fields.Text()
    
    # Budget
    budget_available = fields.Float()
    
    def create_purchase_order(self):
        """
        Convert internal requisition → PO
        Procurement team reviews and converts
        """
        po = self.env['purchase.order'].create({
            'order_line': [
                (0, 0, {
                    'product_id': line.product_id.id,
                    'product_qty': line.product_qty,
                })
                for line in self.line_ids
            ]
        })
        return po
```

### **8. AUTOMATED REPLENISHMENT**
```python
class StockRule(models.Model):
    """Auto-create POs when stock low"""
    
    action = fields.Selection([
        ('pull', 'Pull from supplier (Create PO)'),
        ('push', 'Push to location'),
    ])
    
    # Trigger conditions
    location_src_id = fields.Many2one('stock.location')  # Supplier
    location_dest_id = fields.Many2one('stock.location')  # Our stock
    
    # Min/max levels
    minimum_qty = fields.Float()  # Trigger PO when < this
    maximum_qty = fields.Float()  # Order to reach this

def create_purchase_orders(self):
    """
    Inventory check (e.g., daily cron):
    For each product with purchase rules:
        If on_hand < minimum_qty:
            qty_to_order = maximum_qty - on_hand
            Create PO from preferred supplier
    """
    for rule in self.env['stock.rule'].search([('action', '=', 'pull')]):
        product = rule.product_id
        
        on_hand = product.qty_available
        
        if on_hand < rule.minimum_qty:
            qty_to_order = rule.maximum_qty - on_hand
            
            # Get preferred supplier
            supplier = product.supplier_ids[0] if product.supplier_ids else None
            
            if supplier:
                po = self.env['purchase.order'].create({
                    'partner_id': supplier.partner_id.id,
                    'order_line': [(0, 0, {
                        'product_id': product.id,
                        'product_qty': qty_to_order,
                        'price_unit': supplier.price,
                    })]
                })
                po.action_rfq_confirm()  # Auto-confirm if < limit
```

### **9. GOODS RECEIPT NOTE (GRN) & RECEIVING**
```python
"""
GRN = Physical receipt of goods from supplier
Creates stock.picking to record receipt
"""

class StockPicking(models.Model):
    
    purchase_id = fields.Many2one('purchase.order')
    
    # Receiving workflow
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
        ('assigned', 'Ready to Receive'),
        ('done', 'Received'),
    ])

# Receiving flow:
# 1. Supplier ships goods
# 2. Warehouse receives package
# 3. Scan GRN picking (from PO)
# 4. Confirm quantities received
# 5. If over/short from PO → Create variance record
# 6. Move validated → stock updated, PO line qty_received updated

def record_receipt(self, picking_id, received_qty):
    """
    Record goods received
    """
    picking = self.env['stock.picking'].browse(picking_id)
    
    for move in picking.move_ids:
        # Create move line with actual qty
        self.env['stock.move.line'].create({
            'move_id': move.id,
            'product_id': move.product_id.id,
            'qty_done': received_qty,
        })
    
    picking.button_validate()  # Finalize receipt
    
    # Update PO line qty_received
    po_line = picking.purchase_id.order_line[0]
    po_line.qty_received = received_qty
    
    # Flag variance if received != PO qty
    if received_qty != move.product_qty:
        self.env['purchase.order.variance'].create({
            'purchase_order_id': picking.purchase_id.id,
            'variance_type': 'quantity',
            'expected': move.product_qty,
            'actual': received_qty,
        })
```

### **10. PURCHASE ANALYTICS & REPORTING**
```python
class PurchaseReport(models.TransientModel):
    """Purchase analytics"""
    
    @api.model
    def get_spend_by_vendor(self, date_from, date_to):
        """Total spend per vendor"""
        pos = self.env['purchase.order'].search([
            ('date_order', '>=', date_from),
            ('date_order', '<=', date_to),
            ('state', '!=', 'cancel'),
        ])
        
        spend = {}
        for po in pos:
            vendor = po.partner_id.name
            if vendor not in spend:
                spend[vendor] = 0
            spend[vendor] += po.amount_total
        
        return spend
    
    @api.model
    def get_on_time_delivery(self, vendor_id):
        """% of on-time deliveries"""
        pos = self.env['purchase.order'].search([
            ('partner_id', '=', vendor_id),
            ('state', '=', 'done'),
        ])
        
        on_time = 0
        for po in pos:
            for picking in po.picking_ids:
                if picking.state == 'done':
                    if picking.date_done <= po.date_planned:
                        on_time += 1
        
        percentage = (on_time / len(pos)) * 100 if pos else 0
        return percentage
```

---

## 📚 **KEY MODELS CHEAT SHEET**

| Model | Purpose |
|-------|---------|
| **purchase.order** | Purchase order |
| **purchase.order.line** | PO line item |
| **purchase.requisition** | RFQ request |
| **account.move** | Vendor bill |
| **stock.picking** | GRN (receipt) |
| **product.supplierinfo** | Vendor pricing |
| **vendor.scorecard** | Vendor rating |

---

## 🔧 **COMMON TASKS**

### **Create PO from RFQ**
```python
rfq = self.env['purchase.order'].browse(rfq_id)
rfq.action_rfq_confirm()  # Convert to PO
```

### **Create Bill from PO**
```python
bill = po.create_bill_from_po(po.order_line)
bill.action_post()  # Post to GL
```

### **Check 3-Way Match**
```python
match_result = po.validate_3way_match()
# Returns variance status and variance messages
```

### **Get Supplier Price**
```python
supplier = self.env['res.partner'].browse(supplier_id)
price = supplier.get_supplier_price(product, qty=100)
```

---

## ⚠️ **CRITICAL RULES**

1. **RFQ → PO**: Must confirm to create stock moves
2. **3-Way Match**: PO qty ≈ GRN qty ≈ Bill qty (±2%)
3. **GRN updates qty_received**: On picking validation
4. **Bill creates GL entries**: When posted to GL
5. **Blanket orders**: Multiple releases, one contract

---

## 🚀 **QUICK REFERENCE**

- **Lead time**: Calculated from supplier_delay + order date
- **Auto-PO**: Stock rules trigger purchase when low
- **Vendor scorecard**: On-time %, quality, responsiveness
- **Tiered pricing**: Min qty triggers different unit prices
- **Variance tracking**: Over/short quantities flagged

---

**Skill Status:** ✅ Ready for all Purchase tasks
