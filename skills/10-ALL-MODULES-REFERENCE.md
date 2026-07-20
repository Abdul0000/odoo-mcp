# Odoo All Modules Reference & Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐ | **Coverage:** 50+ Modules | **Last Updated:** 2024

---

## 🎯 **COMPLETE ODOO MODULE REFERENCE**

### **TIER 1: CORE BUSINESS MODULES (8)**

#### **1. ACCOUNTING (account)**
**Key Models:** `account.move`, `account.move.line`, `account.account`, `account.journal`

**Quick Pattern:**
```python
# Create invoice
invoice = self.env['account.move'].create({
    'move_type': 'out_invoice',
    'partner_id': customer.id,
    'invoice_date': fields.Date.today(),
    'line_ids': [(0, 0, {
        'account_id': account.id,
        'debit': 1000,  # OR credit, not both
        'name': 'Line description',
    })]
})
invoice.action_post()  # Post to GL
```

**Key Features:**
- Invoices (customer, vendor, credit notes, refunds)
- Journal entries & GL posting
- Tax calculation & management
- Bank reconciliation
- Multi-currency & exchange rates
- Fiscal periods & reporting

**Integration:** All modules → GL entries

---

#### **2. SALES (sale)**
**Key Models:** `sale.order`, `sale.order.line`, `account.move` (linked)

**Quick Pattern:**
```python
so = self.env['sale.order'].create({
    'partner_id': customer.id,
    'order_line': [(0, 0, {
        'product_id': product.id,
        'product_uom_qty': 10,
        'price_unit': 100,
    })]
})
so.action_confirm()  # → Creates picking, may create invoice
```

**Key Features:**
- Quotations & sales orders
- Invoicing policies (order, delivery, usage)
- Subscriptions & recurring
- Commissions
- Discounts & pricelists
- Customer portal

---

#### **3. PURCHASE (purchase)**
**Key Models:** `purchase.order`, `purchase.order.line`, `purchase.requisition`

**Quick Pattern:**
```python
po = self.env['purchase.order'].create({
    'partner_id': vendor.id,
    'order_line': [(0, 0, {
        'product_id': product.id,
        'product_qty': 100,
    })]
})
po.action_rfq_confirm()  # Draft → PO
```

**Key Features:**
- RFQs & purchase orders
- Vendor quotes & comparison
- Blanket orders
- Auto-PO from reorder rules
- 3-way match (PO→GRN→Bill)
- Vendor evaluation

---

#### **4. INVENTORY (stock)**
**Key Models:** `stock.move`, `stock.picking`, `stock.quant`, `stock.location`

**Quick Pattern:**
```python
picking = self.env['stock.picking'].create({
    'picking_type_id': warehouse.out_type_id.id,
    'move_ids': [(0, 0, {
        'product_id': product.id,
        'product_qty': 10,
        'location_id': warehouse.lot_stock_id.id,
        'location_dest_id': CUSTOMER_LOCATION.id,
    })]
})
picking.action_confirm()
picking.button_validate()  # Execute move
```

**Key Features:**
- Stock movements & locations
- Multi-step workflows (1/2/3-step)
- Reservations & backorders
- Lot/serial tracking
- Cycle counts & adjustments
- Warehouse configuration
- Stock valuations (FIFO/LIFO/Average)

---

#### **5. MANUFACTURING (mrp)**
**Key Models:** `mrp.production`, `mrp.bom`, `mrp.workcenter`, `quality.check`

**Quick Pattern:**
```python
mo = self.env['mrp.production'].create({
    'product_id': product.id,
    'product_qty': 100,
    'bom_id': bom.id,
})
mo.action_confirm()  # Creates moves
mo.action_start()    # Creates work orders
```

**Key Features:**
- Bill of Materials (single & multi-level)
- Production workflows
- Work orders & routing
- Quality control & scrap
- Subcontracting
- MPS & master scheduling
- Production costing

---

#### **6. HR (hr)**
**Key Models:** `hr.employee`, `hr.contract`, `hr.leave`, `hr.attendance`

**Quick Pattern:**
```python
employee = self.env['hr.employee'].create({
    'name': 'John Doe',
    'active': True,
})
contract = self.env['hr.contract'].create({
    'employee_id': employee.id,
    'date_start': '2024-01-01',
    'wage': 50000,
    'struct_id': structure.id,
})
contract.action_activate()
```

**Key Features:**
- Employee management
- Contracts & positions
- Leave & attendance
- Organizational chart
- Skills & certifications
- Document management

---

#### **7. PAYROLL (hr_payroll)**
**Key Models:** `hr.payslip`, `hr.salary.rule`, `hr.salary.structure`, `hr.payslip.run`

**Quick Pattern:**
```python
run = self.env['hr.payslip.run'].create({
    'date_start': '2024-09-01',
    'date_end': '2024-09-30',
})
run.action_draft_payslips()  # Create payslips for all active employees

payslip = run.slip_ids[0]
payslip.compute_sheet()  # Calculate salary
payslip.action_payslip_done()  # Lock & post GL
```

**Key Features:**
- Payroll runs & slips
- Salary structures & rules
- Tax & statutory deductions
- Salary advances & loans
- Expense claims
- Payroll reports

---

#### **8. CRM (crm)**
**Key Models:** `crm.lead`, `crm.stage`, `crm.team`, `mail.activity`

**Quick Pattern:**
```python
lead = self.env['crm.lead'].create({
    'name': 'Prospect Corp',
    'email_from': 'contact@prospect.com',
    'expected_revenue': 50000,
})
lead.action_convert_opportunity()  # Lead → Opportunity

# Move through pipeline
lead.stage_id = next_stage
```

**Key Features:**
- Leads & opportunities
- Sales pipeline
- Lead scoring & qualification
- Sales forecasting
- Activity tracking
- Automation rules

---

### **TIER 2: WAREHOUSE & LOGISTICS (4)**

#### **9. SHIPPING (stock_intrastat)**
- Intrastat reporting
- Customs declarations

#### **10. LABEL PRINTING (stock_intrastat)**
- Product labels
- GS1 barcodes
- Multi-format printing

#### **11. BARCODE (barcodes)**
**Key:** `barcode.nomenclature`
```python
# Generate barcode
barcode = self.env['barcode.nomenclature'].get_barcode('EAN13', '1234567890128')
```

#### **12. DELIVERY (delivery)**
**Key Models:** `delivery.carrier`, `stock.picking` (linked)
```python
carrier = self.env['delivery.carrier'].create({
    'name': 'FedEx',
    'delivery_type': 'fixed',
    'fixed_price': 50,
})
# Carrier linked to shipping method
```

---

### **TIER 3: FINANCIAL MODULES (6)**

#### **13. ACCOUNTING REPORTS (account_reports)**
- Profit & Loss
- Balance Sheet
- Trial Balance
- Tax reports

#### **14. EXPENSES (hr_expense)**
**Key Models:** `hr.expense`, `hr.expense.sheet`
```python
expense = self.env['hr.expense'].create({
    'employee_id': employee.id,
    'product_id': product.id,
    'amount': 500,
})
```

#### **15. BUDGET (account_budget)**
- Budget management
- Budget vs actual

#### **16. DOCUMENTS (documents)**
- Document management
- File storage
- Access control

#### **17. APPROVALS (approvals)**
- Approval workflows
- Multi-level approval

#### **18. SIGN (sign)**
**Key:** Template-based e-signing
```python
# E-signature capture for contracts, quotes
```

---

### **TIER 4: SALES & MARKETING (8)**

#### **19. WEBSITE (website)**
- Website builder
- eCommerce storefront
- QWeb templating
- Portal access

#### **20. ECOMMERCE (website_sale)**
**Key Models:** `product.product`, `website`
- Shopping cart
- Checkout flow
- Order processing

#### **21. EMAIL MARKETING (mass_mailing)**
**Key Models:** `mailing.mailing`, `mailing.contact`
```python
campaign = self.env['mailing.mailing'].create({
    'subject': 'Summer Sale',
    'body': 'Check out our deals!',
})
campaign.action_launch()
```

#### **22. MARKETING AUTOMATION (marketing_automation)**
- Campaign workflows
- Lead nurturing
- Automation triggers

#### **23. SURVEYS (survey)**
**Key Models:** `survey.survey`, `survey.question`
```python
survey = self.env['survey.survey'].create({
    'title': 'Customer Satisfaction',
})
```

#### **24. SMS MARKETING (sms)**
- SMS campaigns
- SMS gateways

#### **25. BLOG (blog)**
**Key Models:** `blog.blog`, `blog.post`
```python
blog = self.env['blog.blog'].create({'name': 'My Blog'})
post = self.env['blog.post'].create({
    'blog_id': blog.id,
    'title': 'First Post',
})
```

#### **26. FORUM (forum)**
- Community discussions
- Q&A platform

---

### **TIER 5: PROJECT & SERVICES (7)**

#### **27. PROJECT (project)**
**Key Models:** `project.project`, `project.task`
```python
project = self.env['project.project'].create({
    'name': 'Website Redesign',
})
task = self.env['project.task'].create({
    'project_id': project.id,
    'name': 'Homepage design',
})
```

#### **28. TIMESHEETS (hr_timesheets)**
**Key Models:** `account.analytic.line`
```python
timesheet = self.env['account.analytic.line'].create({
    'employee_id': employee.id,
    'project_id': project.id,
    'task_id': task.id,
    'unit_amount': 8,  # hours
})
```

#### **29. FIELD SERVICE (fieldservice)**
- Service orders
- Technician routing
- Work orders

#### **30. HELP DESK (helpdesk)**
**Key Models:** `helpdesk.ticket`, `helpdesk.sla`
```python
ticket = self.env['helpdesk.ticket'].create({
    'partner_id': customer.id,
    'description': 'Cannot login',
    'priority': '2',
})
```

#### **31. PLANNING (planning)**
- Gantt charts
- Resource planning

#### **32. QUALITY (quality)**
- QA workflows
- Quality checks
- Control points

#### **33. MAINTENANCE (maintenance)**
- Asset maintenance
- Preventive schedules

---

### **TIER 6: STUDIO & CUSTOMIZATION (3)**

#### **34. STUDIO (web_studio)**
- Visual form builder
- No-code customization
- Field & model creation

#### **35. AUTOMATION (automation)**
- Server-side automation
- Cron jobs
- Business rules

#### **36. CODE EDITOR (base_automation)**
- Python automation
- Custom actions

---

### **TIER 7: INTEGRATION (5)**

#### **37. CONNECTORS (connector)**
- Third-party integrations
- API adapters

#### **38. CONTACTS (contacts)**
- Contact management
- Address book

#### **39. CALENDAR (calendar)**
**Key Models:** `calendar.event`
```python
event = self.env['calendar.event'].create({
    'name': 'Meeting',
    'start': '2024-09-20 10:00:00',
    'stop': '2024-09-20 11:00:00',
    'partner_ids': [(6, 0, [partner.id])],
})
```

#### **40. MAIL (mail)**
**Key Models:** `mail.message`, `mail.channel`
```python
msg = self.env['mail.message'].create({
    'body': 'Comment text',
    'model': 'sale.order',
    'res_id': order.id,
})
```

#### **41. DISCUSS (mail_bot)**
- Real-time chat
- Channel messaging

---

### **TIER 8: SECURITY & COMPLIANCE (6)**

#### **42. PERMISSIONS (base)**
- Record rules (ir.rule)
- Field access
- Menu permissions

#### **43. API TOKENS (api_keys)**
```python
# Create API key for third-party apps
```

#### **44. 2FA (auth_totp)**
- Two-factor authentication
- TOTP tokens

#### **45. DATA PROTECTION (privacy_consent)**
- GDPR compliance
- Consent management

#### **46. MOBILE (mobile)**
- Mobile app support
- Offline mode

#### **47. IOT (iot)**
- IoT device integration
- Sensor data

---

### **TIER 9: ADVANCED (7)**

#### **48. KNOWLEDGE (knowledge)**
- Internal knowledge base
- Wiki system

#### **49. EVENTS (event)**
**Key Models:** `event.event`, `event.registration`
```python
event = self.env['event.event'].create({
    'name': 'Conference 2024',
    'date_begin': '2024-10-01',
})
```

#### **50. MEETINGS (calendar_sms)**
- Meeting reminders
- SMS notifications

#### **51. STOCK INTRASTAT (stock_intrastat)**
- Customs declarations
- Intrastat reporting

#### **52. LUNCH (lunch)**
- Employee lunch orders
- Lunch point tracking

#### **53. SHIFTS (hr_shift)**
- Shift scheduling
- Shift patterns

#### **54. PRESENCE (hr_presence)**
- Real-time presence
- In/Out tracking

---

## 🔄 **MODULE DEPENDENCIES CHART**

```
accounting (core)
    ↑
    ├─ sale → account.move (out_invoice)
    ├─ purchase → account.move (in_invoice)
    ├─ stock → account.move (entry)
    ├─ payroll → account.move (salary GL)
    ├─ expenses → account.move (expense entry)
    └─ budget → account.move (budget vs actual)

stock (core)
    ↑
    ├─ sale → stock.move (delivery)
    ├─ purchase → stock.move (receipt)
    ├─ manufacturing → stock.move (consumption/production)
    └─ delivery → stock.picking (shipping)

hr (core)
    ↑
    ├─ payroll → hr.contract (salary structure)
    ├─ timesheets → hr.employee (time tracking)
    ├─ expenses → hr.employee (expense owner)
    └─ leave → hr.employee (leave allocation)

sale (revenue)
    ↑
    ├─ crm → crm.lead (opportunity to SO)
    ├─ project → project.task (service delivery)
    └─ helpdesk → support (after-sales)

purchase (cost)
    ↑
    └─ manufacturing → mrp.production (BOM sourcing)
```

---

## 🚀 **QUICK MODULE LOOKUP**

| Need... | Use Module |
|---------|-----------|
| Customer invoice | Sales (#2) + Accounting (#1) |
| Vendor bill | Purchase (#3) + Accounting (#1) |
| Inventory move | Inventory (#4) + Accounting (#1) |
| Production order | Manufacturing (#5) + Inventory (#4) |
| Employee payroll | Payroll (#7) + HR (#6) |
| Sales lead | CRM (#8) + Sales (#2) |
| Support ticket | Helpdesk (#30) |
| Project task | Project (#27) + Timesheets (#28) |
| Website | Website (#19) + eCommerce (#20) |
| Marketing | Email Marketing (#21) + Surveys (#23) |

---

## 💻 **COMMON DEVELOPMENT PATTERNS**

### **Across All Modules**

```python
# 1. State machines (used in ALL modules)
states = [
    ('draft', 'Draft'),
    ('confirm', 'Confirmed'),
    ('done', 'Done'),
]
state = fields.Selection(states)

# 2. Related documents (linking models)
invoice_id = fields.Many2one('account.move')  # Link to invoice
partner_id = fields.Many2one('res.partner')   # Link to customer

# 3. Amounts & totals (financial calculation)
@api.depends('line_ids')
def _compute_total(self):
    self.total = sum(l.amount for l in self.line_ids)

# 4. Date tracking (audit trail)
create_date = fields.Datetime()   # Created
write_date = fields.Datetime()    # Last modified
date_order = fields.Datetime()    # Reference date

# 5. GL posting (accounting integration)
move_id = fields.Many2one('account.move')  # GL entry
def action_post(self):
    self.move_id = create_gl_entry(self)

# 6. Workflow buttons
def action_confirm(self):
    self.state = 'confirmed'

def action_post(self):
    self.state = 'posted'
```

---

## 📊 **MODULE STATISTICS**

| Tier | Name | Modules | Complexity | Usage |
|------|------|---------|-----------|-------|
| 1 | Core Business | 8 | ⭐⭐⭐⭐⭐ | 100% |
| 2 | Logistics | 4 | ⭐⭐⭐ | 60% |
| 3 | Financial | 6 | ⭐⭐⭐⭐ | 70% |
| 4 | Sales & Mkt | 8 | ⭐⭐⭐ | 80% |
| 5 | Services | 7 | ⭐⭐⭐⭐ | 40% |
| 6 | Studio | 3 | ⭐⭐ | 50% |
| 7 | Integration | 5 | ⭐⭐⭐ | 30% |
| 8 | Security | 6 | ⭐⭐⭐⭐ | 40% |
| 9 | Advanced | 7 | ⭐⭐ | 20% |

**Total: 54 Modules** (v16-19)

---

## ✅ **IMPLEMENTATION CHECKLIST**

### **Minimum (MVP)**
- [x] Accounting
- [x] Sales
- [x] Purchase
- [x] Inventory

### **Standard**
- [x] HR & Payroll
- [x] CRM
- [x] Manufacturing (if needed)
- [x] Helpdesk (if needed)

### **Advanced**
- [x] Project & Timesheets
- [x] Email Marketing
- [x] Field Service (if needed)
- [x] Quality (if needed)

---

**Skill Status:** ✅ Ready for all module queries

