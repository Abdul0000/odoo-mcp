# Odoo Sales Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE PATTERNS & WORKFLOWS

### **1. SALES ORDER LIFECYCLE**
```python
# Workflow: Quotation → SO → Delivery → Invoice → Payment

class SaleOrder(models.Model):
    _name = 'sale.order'
    
    name = fields.Char(auto=True)  # SO-2024-001
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('sent', 'Quotation Sent'),
        ('sale', 'Sales Order'),
        ('done', 'Done'),
        ('cancel', 'Cancelled')
    ])
    
    order_line = fields.One2many('sale.order.line', 'order_id')
    
    partner_id = fields.Many2one('res.partner', required=True)
    currency_id = fields.Many2one('res.currency')
    company_id = fields.Many2one('res.company')
    
    # Dates
    date_order = fields.Datetime(default=fields.Datetime.now)
    commitment_date = fields.Date()  # Expected delivery
    
    # Pricing
    amount_untaxed = fields.Float(compute='_compute_amounts')
    amount_tax = fields.Float(compute='_compute_amounts')
    amount_total = fields.Float(compute='_compute_amounts')
    
    # Payment term
    payment_term_id = fields.Many2one('account.payment.term')
    
    # Linked documents
    picking_ids = fields.One2many('stock.picking', 'sale_id')
    invoice_ids = fields.One2many('account.move', 'sale_id')
    
    # Salesperson commission
    user_id = fields.Many2one('res.users')  # Sales person
    team_id = fields.Many2one('crm.team')

# LIFECYCLE TRANSITIONS

def action_quotation_send(self):
    """Send quotation to customer"""
    # Create portal access for customer
    # Send email with quote PDF
    self.state = 'sent'

def action_confirm(self):
    """
    Convert quotation → Sales Order
    - Lock amounts & lines
    - Create stock moves (pick/delivery)
    - Generate invoice (if policy = 'order')
    """
    # Create stock picking
    for line in self.order_line:
        if line.product_id.type == 'product':
            self.env['stock.picking'].create({
                'picking_type_id': warehouse.out_type_id.id,
                'sale_id': self.id,
                'move_ids': [(0, 0, {
                    'product_id': line.product_id.id,
                    'product_qty': line.product_uom_qty,
                    'sale_line_id': line.id,
                })]
            })
    
    self.state = 'sale'

def _create_invoices(self):
    """
    Create invoice based on invoicing policy
    (manual / delivery / usage)
    """
    invoice_lines = []
    for line in self.order_line:
        invoice_lines.append((0, 0, {
            'product_id': line.product_id.id,
            'quantity': line.product_uom_qty,
            'price_unit': line.price_unit,
            'tax_ids': [(6, 0, line.tax_id.ids)],
        }))
    
    invoice = self.env['account.move'].create({
        'move_type': 'out_invoice',
        'sale_id': self.id,
        'partner_id': self.partner_id.id,
        'line_ids': invoice_lines,
    })
    
    return invoice
```

### **2. SALES ORDER LINES & PRICING**
```python
class SaleOrderLine(models.Model):
    _name = 'sale.order.line'
    
    order_id = fields.Many2one('sale.order', required=True)
    
    # Product & quantity
    product_id = fields.Many2one('product.product', required=True)
    product_uom_qty = fields.Float(required=True)
    product_uom = fields.Many2one('uom.uom')
    
    # Pricing
    price_unit = fields.Float()  # Unit price
    discount = fields.Float()  # % discount (e.g., 10.0 for 10%)
    
    # Calculated
    price_subtotal = fields.Float(compute='_compute_amount_line')
    price_tax = fields.Float(compute='_compute_amount_line')
    price_total = fields.Float(compute='_compute_amount_line')
    
    # Tax
    tax_id = fields.Many2many('account.tax')
    
    # Customer-specific details
    name = fields.Text()  # Description
    
    # Fulfillment
    state = fields.Selection([
        ('draft', 'Draft'),
        ('cancel', 'Cancelled'),
        ('sale', 'Confirmed'),
        ('done', 'Delivered'),
    ])
    
    qty_delivered = fields.Float(compute='_compute_qty_delivered')  # From pickings
    qty_invoiced = fields.Float(compute='_compute_qty_invoiced')  # From invoices
    qty_to_invoice = fields.Float(compute='_compute_qty_to_invoice')
    
    # Recurring
    is_recurring = fields.Boolean()
    recurring_start_date = fields.Date()
    recurring_next_date = fields.Date()
    recurring_invoice = fields.Boolean()
    
    @api.depends('product_uom_qty', 'price_unit', 'discount', 'tax_id')
    def _compute_amount_line(self):
        """Calculate totals for line"""
        for line in self:
            # Price after discount
            price = line.price_unit * (1 - line.discount / 100)
            
            # Subtotal
            subtotal = price * line.product_uom_qty
            
            # Tax
            tax_amount = 0
            for tax in line.tax_id:
                if tax.amount_type == 'percent':
                    tax_amount += subtotal * (tax.amount / 100)
            
            line.price_subtotal = subtotal
            line.price_tax = tax_amount
            line.price_total = subtotal + tax_amount

# INVOICING POLICIES
class SaleOrder(models.Model):
    
    invoice_status = fields.Selection([
        ('upselling', 'Upselling Opportunity'),
        ('invoiced', 'Fully Invoiced'),
        ('to invoice', 'To Invoice'),
        ('no', 'Nothing to Invoice')
    ])
    
    invoicing_policy = fields.Selection([
        ('order', 'Invoice all quantities ordered'),
        ('delivery', 'Invoice delivered quantities'),
        ('usage', 'Invoice on timesheet (services)'),
    ])

# Policy LOGIC:
# 1. 'order': Create invoice when SO confirmed
# 2. 'delivery': Create invoice only when goods picked/delivered
# 3. 'usage': For services - invoice based on actual work done (timesheets)
```

### **3. QUOTATIONS & TEMPLATES**
```python
class SaleOrderTemplate(models.Model):
    _name = 'sale.order.template'
    
    name = fields.Char(required=True)  # 'Standard Package', 'Enterprise'
    
    # Template lines
    sale_order_template_line_ids = fields.One2many(
        'sale.order.template.line', 'sale_order_template_id'
    )
    
    # Company & currency
    company_id = fields.Many2one('res.company')
    currency_id = fields.Many2one('res.currency')

class SaleOrderTemplateLine(models.Model):
    _name = 'sale.order.template.line'
    
    sale_order_template_id = fields.Many2one('sale.order.template')
    
    product_id = fields.Many2one('product.product')
    name = fields.Text()
    quantity = fields.Float()
    price_unit = fields.Float()

# USING TEMPLATES
def create_quotation_from_template(self, template_id, customer):
    """Generate SO from template"""
    template = self.env['sale.order.template'].browse(template_id)
    
    lines = []
    for tpl_line in template.sale_order_template_line_ids:
        lines.append((0, 0, {
            'product_id': tpl_line.product_id.id,
            'name': tpl_line.name,
            'product_uom_qty': tpl_line.quantity,
            'price_unit': tpl_line.price_unit,
        }))
    
    so = self.env['sale.order'].create({
        'partner_id': customer.id,
        'order_line': lines,
    })
    
    return so
```

### **4. DISCOUNTS & PROMOTIONS**
```python
class SaleOrder(models.Model):
    
    # Line-level discount (in SaleOrderLine)
    # discount = fields.Float()  # 10.0 = 10% off
    
    # Order-level discount
    pricelist_id = fields.Many2one('product.pricelist')

class ProductPricelist(models.Model):
    _name = 'product.pricelist'
    
    name = fields.Char(required=True)  # 'Wholesale', 'VIP'
    
    # Pricelists have rules
    item_ids = fields.One2many('product.pricelist.item', 'pricelist_id')

class ProductPricelistItem(models.Model):
    _name = 'product.pricelist.item'
    
    pricelist_id = fields.Many2one('product.pricelist')
    
    # When to apply
    product_id = fields.Many2one('product.product')  # Specific product
    product_tmpl_id = fields.Many2one('product.template')  # Product family
    category_id = fields.Many2one('product.category')  # Entire category
    
    # Quantity-based
    min_quantity = fields.Float()  # Apply if qty >= this
    
    # How to discount
    compute_price = fields.Selection([
        ('fixed', 'Fixed Price'),
        ('percentage', 'Percentage'),
        ('formula', 'Formula'),
    ])
    
    fixed_price = fields.Float()
    percent_price = fields.Float()  # -15 for 15% discount
    
    # Priority (lower = checked first)
    sequence = fields.Integer()

# PRICELIST LOGIC
def get_price_unit(self, product, qty, partner, pricelist):
    """
    Get price after applying pricelist
    """
    # Find matching rule
    rule = pricelist.item_ids.search([
        ('product_id', '=', product.id),
        ('min_quantity', '<=', qty),
    ], limit=1, order='sequence')
    
    if rule:
        if rule.compute_price == 'fixed':
            return rule.fixed_price
        elif rule.compute_price == 'percentage':
            base_price = product.list_price
            return base_price * (1 + rule.percent_price / 100)
    
    return product.list_price
```

### **5. DOWN PAYMENTS & PARTIAL INVOICING**
```python
class SaleOrder(models.Model):
    
    # Down payment support
    def action_create_down_payment(self, percent):
        """
        Create invoice for % of total amount
        E.g., 30% deposit before production
        """
        dp_amount = self.amount_total * (percent / 100)
        
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'sale_id': self.id,
            'partner_id': self.partner_id.id,
            'line_ids': [(0, 0, {
                'name': f'Down Payment ({percent}%)',
                'account_id': DOWN_PAYMENT_ACCOUNT.id,
                'price_unit': dp_amount,
                'quantity': 1,
            })]
        })
        
        return invoice

# PARTIAL INVOICING
class SaleOrderLine(models.Model):
    
    qty_to_invoice = fields.Float(compute='_compute_qty_to_invoice')
    
    def _compute_qty_to_invoice(self):
        """Based on invoicing policy"""
        for line in self:
            if line.order_id.invoicing_policy == 'order':
                line.qty_to_invoice = line.product_uom_qty
            elif line.order_id.invoicing_policy == 'delivery':
                # Only qty that's been picked
                line.qty_to_invoice = line.qty_delivered - line.qty_invoiced
```

### **6. RECURRING & SUBSCRIPTION ORDERS**
```python
class SaleOrder(models.Model):
    
    is_subscription = fields.Boolean()
    subscription_id = fields.Many2one('sale.subscription')

class SaleSubscription(models.Model):
    _name = 'sale.subscription'
    
    name = fields.Char(auto=True)
    partner_id = fields.Many2one('res.partner')
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('paused', 'Paused'),
        ('closed', 'Closed'),
        ('cancelled', 'Cancelled')
    ])
    
    # Recurring info
    recurring_next_date = fields.Date()
    recurring_invoice_day = fields.Integer(default=15)  # Day of month
    
    subscription_line_ids = fields.One2many(
        'sale.subscription.line', 'subscription_id'
    )
    
    # Auto-create invoices
    def action_create_recurring_invoice(self):
        """Create monthly invoice for subscription"""
        invoice_lines = []
        for line in self.subscription_line_ids:
            invoice_lines.append((0, 0, {
                'product_id': line.product_id.id,
                'quantity': line.quantity,
                'price_unit': line.price_unit,
            }))
        
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'line_ids': invoice_lines,
        })
        
        # Schedule next invoice
        self.recurring_next_date = fields.Date.add(
            fields.Date.today(),
            days=30
        )
        
        return invoice

class SaleSubscriptionLine(models.Model):
    _name = 'sale.subscription.line'
    
    subscription_id = fields.Many2one('sale.subscription')
    product_id = fields.Many2one('product.product')
    quantity = fields.Float()
    price_unit = fields.Float()
    
    # Billing period
    quantity_max = fields.Float()  # Cap usage
```

### **7. SALES COMMISSIONS**
```python
class SaleOrder(models.Model):
    
    # Commission fields
    user_id = fields.Many2one('res.users')  # Salesperson
    team_id = fields.Many2one('crm.team')

class ResPartner(models.Model):
    
    # Commission structure assigned to rep
    grade_id = fields.Many2one('sales.grade')

class SalesGrade(models.Model):
    _name = 'sales.grade'
    
    name = fields.Char()  # 'Junior', 'Senior'
    
    # Commission rules
    commission_rule_ids = fields.One2many(
        'sales.commission.rule', 'grade_id'
    )

class SalesCommissionRule(models.Model):
    _name = 'sales.commission.rule'
    
    grade_id = fields.Many2one('sales.grade')
    
    # Threshold
    min_amount = fields.Float()  # Commission if > this
    commission_percentage = fields.Float()  # Percentage of sale

# CALCULATE COMMISSION
def calculate_commission(self, salesperson, order_amount):
    """
    Based on salesperson grade + order amount
    """
    grade = salesperson.grade_id
    
    # Find applicable rule
    rule = grade.commission_rule_ids.search([
        ('min_amount', '<=', order_amount),
    ], limit=1, order='-min_amount')
    
    if rule:
        commission = order_amount * (rule.commission_percentage / 100)
        return commission
    
    return 0

# Create commission records
class SalesCommission(models.Model):
    _name = 'sales.commission'
    
    salesperson_id = fields.Many2one('res.users')
    order_id = fields.Many2one('sale.order')
    
    amount = fields.Float(compute='_compute_amount')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('paid', 'Paid')
    ])
```

### **8. CUSTOMER PORTAL & E-SIGNATURES**
```python
class SaleOrder(models.Model):
    
    # Portal access
    access_token = fields.Char()  # URL-safe token
    
    portal_signature = fields.Binary()  # Signed by customer
    portal_signature_date = fields.Datetime()
    
    def action_generate_portal_link(self):
        """Create portal link for customer to view/sign"""
        if not self.access_token:
            import secrets
            self.access_token = secrets.token_urlsafe(16)
        
        portal_url = f"/my/orders/{self.id}/{self.access_token}"
        return portal_url

# Portal Controller (website side)
class PortalSaleOrder(controllers.Controller):
    
    @http.route('/my/orders/<model("sale.order"):order>/<access_token>',
               type='http', auth='public')
    def order_portal(self, order, access_token):
        """View SO in portal"""
        if order.access_token != access_token:
            raise Forbidden()
        
        return request.render('sale.order_portal_template', {
            'order': order,
        })

# E-signature capture
class SaleOrder(models.Model):
    
    def action_capture_signature(self, signature_image):
        """Save customer signature"""
        import base64
        self.portal_signature = base64.b64encode(signature_image)
        self.portal_signature_date = fields.Datetime.now()
```

### **9. DELIVERY & INVOICING WORKFLOW**
```python
"""
Complete flow: SO → Picking → Delivery → Invoice → Payment

Scenario: Delivery before invoicing (invoice_policy = 'delivery')
"""

# 1. SO created
sale_order = self.env['sale.order'].create({...})
sale_order.action_confirm()  # Creates picking

# 2. Warehouse picks & delivers
picking = sale_order.picking_ids[0]
picking.action_assign()  # Reserve stock
picking.button_validate()  # Mark as done → stock moved

# 3. Invoice created (auto if policy = 'order')
# OR manual if policy = 'delivery'
sale_order._create_invoices()  # Creates account.move

# 4. Customer pays invoice
payment = self.env['account.payment'].create({...})
payment.action_post()  # Creates GL entry, marks invoice paid
```

### **10. CUSTOMER CREDIT LIMITS**
```python
class ResPartner(models.Model):
    
    credit_limit = fields.Float()  # Max credit amount
    total_credit = fields.Float(compute='_compute_credit')  # Current AR

class SaleOrder(models.Model):
    
    def action_confirm(self):
        """Check credit before confirming"""
        partner = self.partner_id
        
        if partner.total_credit + self.amount_total > partner.credit_limit:
            raise ValidationError(
                f"Credit limit exceeded for {partner.name}. "
                f"Available: ${partner.credit_limit - partner.total_credit}"
            )
        
        # Proceed with confirmation
        super().action_confirm()
```

---

## 📚 **KEY MODELS CHEAT SHEET**

| Model | Purpose |
|-------|---------|
| **sale.order** | Sales order/quotation |
| **sale.order.line** | Line item |
| **account.move** | Invoice |
| **product.pricelist** | Pricing rules |
| **sale.subscription** | Recurring orders |
| **stock.picking** | Delivery |
| **sales.commission** | Rep commission |

---

## 🔧 **COMMON TASKS**

### **Create SO Programmatically**
```python
so = self.env['sale.order'].create({
    'partner_id': customer.id,
    'order_line': [(0, 0, {
        'product_id': product.id,
        'product_uom_qty': 10,
        'price_unit': 100,
    })]
})
so.action_confirm()  # Convert to SO
```

### **Send Quotation**
```python
so.action_quotation_send()  # Sends email to customer
```

### **Create Invoice from SO**
```python
invoices = so._create_invoices()
```

---

## ⚠️ **CRITICAL RULES**

1. **Invoicing policy**: Determines when invoice created
2. **Quotation → SO**: Must confirm to lock prices
3. **Pricelist applied**: At SO line level
4. **Stock moves auto-created**: On SO confirmation (if product)
5. **Portal access**: Token-based URL for customers

---

## 🚀 **QUICK REFERENCE**

- **Down payments**: Create interim invoice before delivery
- **Recurring**: Auto-create invoices on schedule
- **Portal**: Public access to SO without login
- **Commissions**: Calculate on order amount/performance
- **Credit check**: Verify limits before confirming

---

**Skill Status:** ✅ Ready for all Sales tasks
