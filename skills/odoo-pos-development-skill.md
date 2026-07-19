---
name: odoo-pos-development
description: Build Odoo POS systems (v16-19) with session management, custom payment methods, receipt templates, POS extensions, hardware integration, and restaurant features. Short, powerful reference for POS module customization.
---

# Odoo Point of Sale (POS)

## POS Module Setup (5 Min)

**Directory Structure**
```
pos_custom/
  ├── __manifest__.py
  ├── models/
  │   ├── __init__.py
  │   └── pos_session.py
  ├── views/
  │   └── pos_config_views.xml
  ├── static/src/js/
  │   └── pos_custom.js
  └── static/src/css/
      └── pos_custom.scss
```

**__manifest__.py**
```python
{
    'name': 'POS Custom',
    'depends': ['point_of_sale', 'sale'],
    'data': ['views/pos_config_views.xml'],
    'assets': {
        'point_of_sale.assets': [
            'pos_custom/static/src/js/pos_custom.js',
            'pos_custom/static/src/css/pos_custom.scss',
        ],
    },
}
```

## POS Session Management

**Create Session**
```
POS → Sessions → + New
- Select POS config (payment methods, receipts)
- Start session: Opening cash count
- End session: Closing cash, payment reconciliation
```

**Session Model (Extend)**
```python
# models/pos_session.py
from odoo import models, fields

class PosSession(models.Model):
    _inherit = 'pos.session'
    
    daily_sales_target = fields.Float('Daily Target', default=0)
    discount_total = fields.Float('Total Discounts', compute='_compute_discount')
    
    def _compute_discount(self):
        for session in self:
            orders = session.order_ids.filtered(lambda o: o.state != 'draft')
            session.discount_total = sum(orders.mapped('total_discount'))
    
    def action_pos_session_open(self):
        res = super().action_pos_session_open()
        # Custom logic on session open
        return res
```

## POS Config & Payment Methods

**Configure POS Terminal**
```
Settings → POS Configurations
- Journal: Cash, card, wallet
- Payment methods: Add custom payment types
- Receipt: Select receipt template
- Hardware: Printers, scales, payment terminals
- Taxes: Auto-apply tax rules
- Employees: Users allowed on this terminal
```

**Custom Payment Method**
```python
# models/account_journal.py
from odoo import models, fields

class AccountJournal(models.Model):
    _inherit = 'account.journal'
    
    loyalty_points_per_rupee = fields.Float('Points per 100 PKR', default=1)
    
class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'
    
    is_custom_method = fields.Boolean('Custom Payment')
    custom_field = fields.Char('Custom Field')
```

## POS Orders & Products

**POS Order Model**
```python
# models/pos_order.py
from odoo import models, fields

class PosOrder(models.Model):
    _inherit = 'pos.order'
    
    loyalty_points_earned = fields.Float('Loyalty Points', compute='_compute_loyalty')
    customer_id = fields.Many2one('res.partner', 'Customer')
    
    def _compute_loyalty(self):
        for order in self:
            # Calculate points: 1 point per 100 PKR
            order.loyalty_points_earned = order.amount_total / 100
    
    def _onchange_partner_id(self):
        # Fetch customer loyalty balance
        if self.customer_id:
            self.customer_id.loyalty_balance  # Display in POS
```

**Configure Products for POS**
```
Sales → Products → Edit → POS
- Available in POS (checkbox)
- Category (for POS filtering)
- Image (displays in POS)
- Barcode (for quick scan)
- Taxes
- Display price vs cost margin
```

## POS JavaScript (Frontend Logic)

**Custom POS Button & Action**
```javascript
// static/src/js/pos_custom.js
odoo.define('pos_custom.CustomButton', function(require) {
    'use strict';

    const { PosGlobalState } = require('point_of_sale.models');
    const Registries = require('point_of_sale.Registries');

    // Add custom action button
    const CustomActionButton = {
        handler: async () => {
            console.log('Custom action triggered');
            // Custom business logic
            alert('Processing custom action...');
        },
        condition: function() { return true; }
    };

    Registries.ActionPadButton.add(CustomActionButton);

    // Hook into payment
    const PosOrderExtended = (models) => class extends models.Order {
        async pay() {
            console.log('Payment processing for order:', this.uid);
            // Custom pre-payment logic
            return super.pay();
        }
    };
    models.Order = PosOrderExtended(models);
});
```

**Order Line Override**
```javascript
odoo.define('pos_custom.OrderLine', function(require) {
    const { Order } = require('point_of_sale.models');
    const { Orderline } = require('point_of_sale.models');

    const OrderlineExtended = (models) => class extends Orderline {
        constructor(attr, options) {
            super(attr, options);
            this.loyalty_discount = 0;
        }

        apply_loyalty_discount(discount_percent) {
            this.loyalty_discount = this.get_price_with_tax() * (discount_percent / 100);
        }

        get_display_price() {
            return this.get_price_with_tax() - this.loyalty_discount;
        }
    };

    models.Orderline = OrderlineExtended(models);
});
```

## Receipt Templates (QWeb)

**Custom Receipt Layout**
```xml
<!-- views/pos_receipt_template.xml -->
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
  <template id="pos_receipt_custom" inherit_id="point_of_sale.receipt">
    <xpath expr="//div[@class='receipt']" position="replace">
      <div class="receipt">
        <!-- Header -->
        <div class="receipt-header text-center">
          <h3>Your Company Name</h3>
          <p>Location: <t t-esc="pos.session_id.config_id.name"/></p>
          <p>Invoice #: <t t-esc="pos.name"/></p>
        </div>

        <!-- Items -->
        <table class="receipt-items">
          <thead>
            <tr>
              <th>Item</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Total</th>
            </tr>
          </thead>
          <tbody>
            <t t-foreach="pos.lines" t-as="line">
              <tr>
                <td><t t-esc="line.product_id.name"/></td>
                <td><t t-esc="line.qty"/></td>
                <td><t t-esc="line.price_unit"/></td>
                <td><t t-esc="line.price_subtotal"/></td>
              </tr>
            </t>
          </tbody>
        </table>

        <!-- Totals -->
        <div class="receipt-totals">
          <div>Subtotal: PKR <t t-esc="pos.amount_untaxed"/></div>
          <div>Tax: PKR <t t-esc="pos.amount_tax"/></div>
          <div class="font-bold">Total: PKR <t t-esc="pos.amount_total"/></div>
        </div>

        <!-- Payment Info -->
        <div class="receipt-payment">
          <t t-foreach="pos.payment_ids" t-as="payment">
            <div><t t-esc="payment.payment_method_id.name"/>: PKR <t t-esc="payment.amount"/></div>
          </t>
        </div>

        <!-- Footer -->
        <div class="receipt-footer text-center">
          <p>Thank you for your purchase!</p>
          <p>Date: <t t-esc="pos.date_order"/></p>
        </div>
      </div>
    </xpath>
  </template>
</odoo>
```

**Thermal Receipt (80mm)**
```xml
<template id="pos_receipt_thermal" inherit_id="point_of_sale.receipt">
  <xpath expr="//div[@class='receipt']" position="replace">
    <div class="receipt" style="width: 80mm; font-size: 12px; font-family: monospace;">
      <div style="text-align: center; padding: 5mm;">
        <strong>RECEIPT</strong>
        <br/><t t-esc="pos.session_id.config_id.name"/>
        <br/>------------------------------
      </div>
      
      <table style="width: 100%;">
        <t t-foreach="pos.lines" t-as="line">
          <tr>
            <td style="width: 60%;"><t t-esc="line.product_id.name[:15]"/></td>
            <td style="text-align: right;"><t t-esc="'{:.2f}'.format(line.price_subtotal)"/></td>
          </tr>
          <tr>
            <td colspan="2" style="font-size: 10px;">
              <t t-esc="line.qty"/> x <t t-esc="'{:.2f}'.format(line.price_unit)"/>
            </td>
          </tr>
        </t>
      </table>
      
      <div style="text-align: right; padding: 5mm 0;">
        ------------------------------
        <br/>Total: <strong><t t-esc="'{:.2f}'.format(pos.amount_total)"/></strong>
      </div>
    </div>
  </xpath>
</template>
```

## Custom Screens & Views

**Loyalty Points Screen**
```javascript
// Add custom screen to POS
odoo.define('pos_custom.LoyaltyScreen', function(require) {
    const { useContext, useState } = require('web.owl');
    const AbstractAwesomeScreen = require('point_of_sale.AbstractAwesomeScreen');

    class LoyaltyScreen extends AbstractAwesomeScreen {
        setup() {
            super.setup();
            this.state = useState({
                customer: null,
                points_balance: 0,
                points_to_redeem: 0,
            });
        }

        confirm() {
            const { pos } = this.env;
            if (this.state.customer) {
                // Apply loyalty points as discount
                pos.get_order().set_loyalty_discount(this.state.points_to_redeem);
            }
            this.props.resolve({ confirmed: true });
        }
    }

    LoyaltyScreen.template = 'LoyaltyScreenTemplate';
    return LoyaltyScreen;
});
```

## Hardware Integration

**Barcode Scanner**
```javascript
// Detect barcode scan
odoo.define('pos_custom.BarcodeScanning', function(require) {
    const { useBus } = require('web.core');

    useBus(this.env.bus, 'barcode_scanned', (ev) => {
        console.log('Barcode scanned:', ev.detail.barcode);
        // Lookup product and add to cart
        const product = this.env.pos.db.search_product_in_category(-1, ev.detail.barcode);
        if (product) {
            this.env.pos.get_order().add_product(product);
        }
    });
});
```

**Receipt Printer**
```python
# models/iot_device.py
from odoo import models, fields

class IotDevice(models.Model):
    _inherit = 'iot.device'
    
    pos_session_id = fields.Many2one('pos.session', 'POS Session')
    
    def action_print_receipt(self, receipt_html):
        # Send to printer via IoT gateway
        self.send_action({
            'action': 'print',
            'data': receipt_html,
        })
```

**Payment Terminal Integration (Stripe, etc.)**
```python
# controllers/payment_terminal.py
from odoo import http, fields
from odoo.http import request

class PaymentTerminalController(http.Controller):
    
    @http.route('/pos/payment/confirm', type='json', auth='public')
    def confirm_payment(self, session_id, amount, **kw):
        session = request.env['pos.session'].browse(session_id)
        
        # Call payment terminal API
        terminal = session.config_id.payment_terminal_id
        result = terminal.process_payment(amount)
        
        return {
            'success': result['status'] == 'approved',
            'reference': result.get('reference'),
        }
```

## Restaurant Features

**Table Management**
```python
# models/restaurant_table.py
from odoo import models, fields

class RestaurantTable(models.Model):
    _name = 'restaurant.table'
    _description = 'Restaurant Table'
    
    name = fields.Char('Table Name')
    seats = fields.Integer('Seats')
    section_id = fields.Many2one('restaurant.section', 'Section')
    is_available = fields.Boolean('Available', default=True)
    current_order_id = fields.Many2one('pos.order', 'Current Order')
    
    def action_reserve(self):
        self.is_available = False
    
    def action_free(self):
        self.is_available = True
```

**Kitchen Display System (KDS)**
```javascript
// Display orders in kitchen
odoo.define('pos_custom.KitchenScreen', function(require) {
    const AbstractAwesomeScreen = require('point_of_sale.AbstractAwesomeScreen');

    class KitchenScreen extends AbstractAwesomeScreen {
        setup() {
            super.setup();
            this.refresh_orders();
        }

        refresh_orders() {
            const { pos } = this.env;
            const orders = pos.get_order_list().filter(o => o.state === 'paid');
            
            orders.forEach(order => {
                order.lines.forEach(line => {
                    if (!line.prepared) {
                        this.display_item(line);
                    }
                });
            });
        }

        mark_prepared(line_id) {
            // Mark item as prepared
            console.log('Item prepared:', line_id);
        }
    }

    KitchenScreen.template = 'KitchenScreenTemplate';
    return KitchenScreen;
});
```

## Discounts & Loyalty

**Apply Discount**
```python
# models/pos_order.py
class PosOrder(models.Model):
    _inherit = 'pos.order'
    
    def apply_discount(self, discount_percent):
        for line in self.lines:
            line.discount = discount_percent
    
    def apply_coupon(self, coupon_code):
        coupon = self.env['pos.coupon'].search([('code', '=', coupon_code)])
        if coupon:
            self.apply_discount(coupon.discount_percent)
            return True
        return False
```

**Loyalty Program**
```python
# models/loyalty_program.py
from odoo import models, fields

class LoyaltyProgram(models.Model):
    _name = 'loyalty.program'
    
    name = fields.Char('Program Name')
    points_per_amount = fields.Float('Points per 100 PKR', default=1)
    redeem_rate = fields.Float('Redemption Rate (1 point = X PKR)', default=1)
    
class CustomerLoyalty(models.Model):
    _name = 'customer.loyalty'
    
    customer_id = fields.Many2one('res.partner', 'Customer', required=True)
    program_id = fields.Many2one('loyalty.program', 'Program')
    points_balance = fields.Float('Points Balance', default=0)
    
    def redeem_points(self, points):
        if points <= self.points_balance:
            self.points_balance -= points
            return True
        return False
```

## POS Reports

**Daily Sales Report**
```python
# reports/pos_daily_report.py
from odoo import models, fields

class PosDailySalesReport(models.Model):
    _name = 'pos.daily.report'
    _auto = False
    _rec_name = 'date'
    
    date = fields.Date('Date')
    session_id = fields.Many2one('pos.session', 'Session')
    total_sales = fields.Float('Total Sales')
    total_orders = fields.Integer('Orders')
    avg_order_value = fields.Float('Avg Order Value')
    payment_method = fields.Char('Payment Method')
    
    def init(self):
        self.env.cr.execute('''
            CREATE OR REPLACE VIEW pos_daily_report AS (
                SELECT
                    MIN(id) as id,
                    DATE(ps.start_at) as date,
                    ps.id as session_id,
                    SUM(po.amount_total) as total_sales,
                    COUNT(DISTINCT po.id) as total_orders,
                    AVG(po.amount_total) as avg_order_value,
                    apm.name as payment_method
                FROM pos_session ps
                LEFT JOIN pos_order po ON ps.id = po.session_id
                LEFT JOIN account_payment_method apm ON po.payment_method_id = apm.id
                GROUP BY ps.id, date, apm.name
            )
        ''')
```

## POS Styling

```scss
// static/src/css/pos_custom.scss
.pos-screen {
  background-color: #f5f5f5;
}

.orderline {
  border-bottom: 1px solid #ddd;
  padding: 10px;
  
  &:hover {
    background-color: #e8f4f8;
  }
}

.payment-button {
  background-color: #27ae60;
  color: white;
  padding: 15px 30px;
  font-size: 18px;
  border-radius: 5px;
  
  &:hover {
    background-color: #229954;
  }
}

@media (max-width: 768px) {
  .pos-content { font-size: 14px; }
}
```

## Common Patterns

| Task | Code |
|------|------|
| Get current order | `const order = this.env.pos.get_order()` |
| Add product to cart | `order.add_product(product)` |
| Get customer | `order.get_partner()` |
| Calculate total | `order.get_total_with_tax()` |
| Apply discount | `line.set_discount(10)` |
| Get session | `this.env.pos.pos_session` |
| Get products | `this.env.pos.db.get_product_by_id(product_id)` |
| Send to payment | `order.finalize()` |
| Print receipt | `this.env.pos.print_receipt(order)` |
| Get employee | `this.env.pos.user.employee_ids[0]` |

## Z-Report (End-of-Day)

**Generate Z-Report**
```python
# models/z_report.py
from odoo import models, fields

class ZReport(models.Model):
    _name = 'pos.z.report'
    
    session_id = fields.Many2one('pos.session', 'Session', required=True)
    total_sales = fields.Float('Total Sales', compute='_compute_totals')
    total_cash = fields.Float('Cash Collected')
    total_card = fields.Float('Card Payments')
    discounts = fields.Float('Total Discounts')
    taxes = fields.Float('Taxes Collected')
    
    def _compute_totals(self):
        for report in self:
            orders = report.session_id.order_ids.filtered(lambda o: o.state != 'draft')
            report.total_sales = sum(orders.mapped('amount_total'))
    
    def action_close_session(self):
        # Close POS session with Z-report
        self.session_id.action_pos_session_close()
```

## Version Notes (v16-19)

| Feature | v16 | v17+ | v18+ | v19+ |
|---------|-----|------|------|------|
| POS Core | Full | Full | Full | Full |
| Kitchen Display | Basic | Enhanced | Enhanced | Enhanced |
| Loyalty | Add-on | Built-in | Built-in | Built-in |
| Payment API | XML-RPC | JSON-RPC | JSON-RPC | Enhanced |
| Receipt Printer | Basic | Enhanced | Enhanced | Enhanced |
| Restaurant Tables | Add-on | Built-in | Built-in | Built-in |

## Deployment Checklist

```
✓ manifest.py dependencies correct
✓ JS/SCSS syntax validated
✓ Receipt templates render correctly
✓ Barcode scanner tested
✓ Payment terminal configured
✓ Receipt printer connected
✓ POS session tested (open/close)
✓ Customer lookup working
✓ Discounts/loyalty applied correctly
✓ Z-report generated
✓ Backup before production
```

## Quick Refs

**POS Navigation**
- Sessions: POS → Sessions
- Config: Settings → POS Configurations
- Products: Sales → Products
- Reports: POS → Reports → Daily Sales

**Key Models**
- `pos.session` - POS session
- `pos.order` - Order
- `pos.order.line` - Order items
- `pos.payment.method` - Payment types
- `restaurant.table` - Table management
- `loyalty.program` - Loyalty setup

**File Paths**
- JS: `/pos_custom/static/src/js/`
- CSS: `/pos_custom/static/src/css/`
- Templates: `views/pos_*.xml`
- Models: `models/`

**ORM (Quick)**
```python
# Get session
session = env['pos.session'].browse(session_id)

# Get orders
orders = session.order_ids

# Get payment methods
payments = order.payment_ids

# Create order
order = env['pos.order'].create({'name': '/', ...})

# Finalize
order.action_pos_order_done()
```
