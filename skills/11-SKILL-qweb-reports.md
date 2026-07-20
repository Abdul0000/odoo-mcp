# Odoo QWeb Report Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE REPORT PATTERNS

### **1. BASIC QWEB REPORT STRUCTURE**
```xml
<!-- report_invoice.xml -->
<odoo>
    <data>
        <!-- Define report action -->
        <record id="action_report_invoice" model="ir.actions.report">
            <field name="name">Invoice</field>
            <field name="model">account.move</field>
            <field name="report_type">qweb-pdf</field>
            <field name="report_name">module_name.report_invoice</field>
            <field name="report_file">module_name.report_invoice</field>
            <field name="binding_model_id" ref="account.model_account_move"/>
            <field name="binding_type">report</field>
        </record>

        <!-- Define QWeb template -->
        <template id="report_invoice">
            <t t-call="web.html_container">
                <t t-call="web.external_layout">
                    <div class="page">
                        <h2>Invoice</h2>
                        <p t-field="doc.name"/>
                        <p t-field="doc.partner_id.name"/>
                    </div>
                </t t-call>
            </t t-call>
        </template>
    </data>
</odoo>
```

**Key Components:**
- `ir.actions.report` - Registers report in Odoo
- `report_type` - `qweb-pdf` (PDF), `qweb-html` (HTML), `qweb-text` (Text)
- `report_name` - Dot notation: `module.template_id`
- Template inherits `web.external_layout` for page structure
- `doc` variable - Current record being printed

---

### **2. DATA FETCHING FOR REPORTS**

#### **Option A: Use compute_for_report() Method**
```python
# In model (account/models/account_move.py)
class AccountMove(models.Model):
    _name = 'account.move'
    
    def get_invoice_lines_data(self):
        """Fetch report-specific data"""
        # Optimize for report: fetch only needed fields
        return self.env['account.move.line'].search([
            ('move_id', '=', self.id),
            ('exclude_from_invoice_tab', '=', False),
        ]).read(['product_id', 'quantity', 'price_unit', 'tax_ids:sum', 'price_total:sum'])
    
    def _compute_report_subtotal(self):
        """Pre-compute totals for template"""
        self.report_subtotal = sum(l.get('price_total') for l in self.get_invoice_lines_data())
```

**Template Usage:**
```xml
<template id="report_invoice">
    <t t-call="web.html_container">
        <t t-call="web.external_layout">
            <div class="page">
                <!-- Pre-computed value -->
                <p t-field="doc.report_subtotal"/>
                
                <!-- Or fetch in template -->
                <t t-set="lines" t-value="doc.get_invoice_lines_data()"/>
                <table>
                    <tr t-foreach="lines" t-as="line">
                        <td t-esc="line['product_id'][1]"/>
                        <td t-esc="line['quantity']"/>
                    </tr>
                </table>
            </div>
        </t t-call>
    </t t-call>
</template>
```

#### **Option B: Use read_group() for Aggregated Data (See Skill #9)**
```python
# In report model
def get_sales_summary(self, date_from, date_to):
    """Fetch aggregated sales data for report"""
    # Use read_group for performance (See Skill #9)
    result = self.env['sale.order'].read_group(
        [('date_order', '>=', date_from),
         ('date_order', '<=', date_to),
         ('state', '!=', 'cancel')],
        ['team_id', 'amount_total:sum', 'amount_tax:sum'],
        ['team_id']
    )
    return result
```

**Template:**
```xml
<table class="table">
    <tr t-foreach="doc.get_sales_summary(date_from, date_to)" t-as="row">
        <td t-esc="row['team_id'][1]"/>
        <td t-esc="row['amount_total']"/>
        <td t-esc="row['amount_tax']"/>
    </tr>
</table>
```

#### **Option C: Direct SQL Query (See Skill #9)**
```python
# In report model - for complex queries
def get_aging_analysis(self, date_cutoff):
    """Complex AR aging analysis via SQL"""
    self.env.cr.execute("""
        SELECT 
            p.name as partner_name,
            am.name as invoice_number,
            am.date,
            am.amount_total,
            am.amount_residual,
            CASE 
                WHEN DATE_PART('day', NOW() - am.date) < 30 THEN 'Current'
                WHEN DATE_PART('day', NOW() - am.date) BETWEEN 30 AND 60 THEN '30-60 Days'
                WHEN DATE_PART('day', NOW() - am.date) BETWEEN 60 AND 90 THEN '60-90 Days'
                ELSE 'Over 90 Days'
            END as age_bucket
        FROM account_move am
        INNER JOIN res_partner p ON am.partner_id = p.id
        WHERE am.move_type = 'out_invoice'
        AND am.state = 'posted'
        AND am.amount_residual > 0
        ORDER BY p.name, am.date
    """)
    
    columns = ['partner_name', 'invoice_number', 'date', 'amount_total', 'amount_residual', 'age_bucket']
    return [dict(zip(columns, row)) for row in self.env.cr.fetchall()]
```

**Template:**
```xml
<table class="table">
    <thead>
        <tr>
            <th>Partner</th>
            <th>Invoice</th>
            <th>Date</th>
            <th>Amount</th>
            <th>Age</th>
        </tr>
    </thead>
    <tbody>
        <tr t-foreach="doc.get_aging_analysis(cutoff_date)" t-as="row">
            <td t-esc="row['partner_name']"/>
            <td t-esc="row['invoice_number']"/>
            <td t-esc="row['date']" t-options="{'widget': 'date'}"/>
            <td t-esc="row['amount_total']" t-options="{'widget': 'monetary', 'display_currency': currency}"/>
            <td><span t-attf-class="badge badge-#{row['age_bucket'] == 'Current' and 'success' or 'danger'}">
                <t t-esc="row['age_bucket']"/>
            </span></td>
        </tr>
    </tbody>
</table>
```

---

### **3. QWEB TEMPLATE SYNTAX & DIRECTIVES**

```xml
<!-- Data Access & Variables -->
<p t-field="doc.name"/>                           <!-- Field value -->
<p t-esc="doc.amount_total"/>                     <!-- Escape HTML -->
<p t-raw="doc.description"/>                      <!-- Keep HTML tags -->
<p t-set="var_name" t-value="doc.field"/>         <!-- Set variable -->

<!-- Conditions -->
<t t-if="doc.state == 'posted'">
    <span>Posted</span>
</t>
<t t-elif="doc.state == 'draft'">
    <span>Draft</span>
</t>
<t t-else="">
    <span>Other</span>
</t>

<!-- Loops -->
<tr t-foreach="doc.line_ids" t-as="line">
    <td t-esc="line.product_id.name"/>
    <td t-esc="line_index"/>                       <!-- Loop index -->
    <td t-if="line_last">Last item</td>            <!-- Detect last -->
</tr>

<!-- Attributes & String Formatting -->
<p t-attf-class="alert alert-#{doc.state == 'posted' and 'success' or 'warning'}">
    Status: <t t-esc="doc.state"/>
</p>

<!-- String Operations -->
<p t-esc="doc.name[:10]"/>                         <!-- Slice -->
<p t-esc="'Total: %s' % doc.amount_total"/>       <!-- Format -->
<p t-esc="doc.partner_id.name.upper()"/>          <!-- String methods -->

<!-- Calling Methods -->
<p t-esc="doc.get_invoice_lines_data()"/>         <!-- Call Python function -->

<!-- Currency & Formatting -->
<p t-field="doc.amount_total" t-options="{'widget': 'monetary', 'display_currency': currency}"/>

<!-- Date Formatting -->
<p t-field="doc.date_order" t-options="{'widget': 'date'}"/>
<p t-field="doc.date_invoice" t-options="{'format': 'dd/MM/yyyy'}"/>

<!-- Timezone & Localization -->
<p t-esc="doc.create_date" t-options="{'tz': 'Asia/Karachi', 'format': 'medium'}"/>

<!-- Comments -->
<!-- This is a comment, not rendered -->
<t t-comment>
    Multi-line comment
    Not rendered in output
</t>

<!-- Inherit existing template -->
<template id="report_invoice" inherit_id="base.report_invoice">
    <xpath expr="//div[@class='invoice']" position="inside">
        <p>Custom content added here</p>
    </xpath>
</template>
```

---

### **4. REPORT CONTROLLER (PYTHON)**

```python
# In module/controllers/report.py
from odoo import http
from odoo.http import request

class ReportController(http.Controller):
    
    @http.route('/report/<string:report_name>/<int:record_id>', type='http', auth='user')
    def report_pdf(self, report_name, record_id):
        """Custom PDF generation"""
        record = request.env[record_id._name].browse(record_id)
        
        # Render template with context
        context = {
            'doc': record,
            'currency': request.env.user.company_id.currency_id,
            'today': fields.Date.today(),
        }
        
        # Call report method
        report = request.env.ref(f'module_name.{report_name}')
        pdf_content, _ = report._render_qweb_pdf([record.id], data=context)
        
        return request.make_response(
            pdf_content,
            headers=[
                ('Content-Type', 'application/pdf'),
                ('Content-Disposition', f'attachment; filename="{record.name}.pdf"'),
            ]
        )
    
    @http.route('/report/<string:report_name>/preview', type='http', auth='user')
    def report_html(self, report_name, record_id):
        """HTML preview (non-PDF)"""
        record = request.env[record_id._name].browse(record_id)
        
        report = request.env.ref(f'module_name.{report_name}')
        html = report._render_qweb_html([record.id])[0]
        
        return html
```

---

### **5. STYLING & CSS IN REPORTS**

```xml
<template id="report_invoice">
    <t t-call="web.html_container">
        <t t-call="web.external_layout">
            <style>
                .invoice-header {
                    border-bottom: 2px solid #333;
                    padding-bottom: 20px;
                    margin-bottom: 20px;
                }
                
                .line-items table {
                    width: 100%;
                    border-collapse: collapse;
                }
                
                .line-items th {
                    background-color: #f0f0f0;
                    padding: 10px;
                    text-align: left;
                    font-weight: bold;
                }
                
                .line-items td {
                    padding: 8px;
                    border-bottom: 1px solid #ddd;
                }
                
                .line-items tr:hover {
                    background-color: #f9f9f9;
                }
                
                .totals {
                    text-align: right;
                    margin-top: 20px;
                }
                
                .total-amount {
                    font-size: 18px;
                    font-weight: bold;
                    color: #2c3e50;
                }
                
                .page-break {
                    page-break-after: always;
                }
                
                @media print {
                    body {
                        margin: 0;
                        padding: 0;
                    }
                    .no-print {
                        display: none;
                    }
                }
            </style>
            
            <div class="invoice-header">
                <h1>Invoice</h1>
                <p t-field="doc.name"/>
            </div>
            
            <div class="line-items">
                <table>
                    <thead>
                        <tr>
                            <th>Product</th>
                            <th>Quantity</th>
                            <th>Unit Price</th>
                            <th>Total</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr t-foreach="doc.invoice_line_ids" t-as="line">
                            <td t-esc="line.product_id.name"/>
                            <td t-esc="line.quantity"/>
                            <td t-esc="line.price_unit"/>
                            <td t-esc="line.price_total"/>
                        </tr>
                    </tbody>
                </table>
            </div>
            
            <div class="totals">
                <div>Subtotal: <span t-field="doc.amount_untaxed"/></div>
                <div>Tax: <span t-field="doc.amount_tax"/></div>
                <div class="total-amount">
                    Total: <span t-field="doc.amount_total"/>
                </div>
            </div>
        </t t-call>
    </t t-call>
</template>
```

---

### **6. MULTI-PAGE REPORTS WITH PAGE BREAKS**

```xml
<template id="report_multi_page">
    <t t-call="web.html_container">
        <t t-call="web.external_layout">
            <!-- Page 1: Summary -->
            <div class="page">
                <h2>Sales Report - Summary</h2>
                <table class="table">
                    <tr t-foreach="doc.get_sales_summary()" t-as="row">
                        <td t-esc="row['team_id'][1]"/>
                        <td t-esc="row['amount_total']"/>
                    </tr>
                </table>
            </div>
            
            <!-- Page Break -->
            <div class="page-break"/>
            
            <!-- Page 2: Details -->
            <div class="page">
                <h2>Sales Report - Details</h2>
                <table class="table">
                    <tr t-foreach="doc.get_sales_details()" t-as="sale">
                        <td t-esc="sale.name"/>
                        <td t-esc="sale.partner_id.name"/>
                        <td t-esc="sale.amount_total"/>
                    </tr>
                </table>
            </div>
            
            <!-- Page 3: Analysis -->
            <div class="page">
                <h2>Sales Report - Analysis</h2>
                <p>Key metrics and insights</p>
            </div>
        </t t-call>
    </t t-call>
</template>
```

---

### **7. DYNAMIC PARAMETERS IN REPORTS**

```python
# In model report or controller
class ReportSalesAnalysis(models.AbstractModel):
    _name = 'report.module_name.report_sales_analysis'
    _description = 'Sales Analysis Report'
    
    @api.model
    def _get_report_values(self, docids, data=None):
        """
        Called when report is rendered
        data = parameters passed from wizard
        """
        
        # Default parameters
        date_from = data.get('date_from', fields.Date.today())
        date_to = data.get('date_to', fields.Date.today())
        team_ids = data.get('team_ids', [])
        
        # Fetch data using read_group (See Skill #9)
        sales_data = self.env['sale.order'].read_group(
            [('date_order', '>=', date_from),
             ('date_order', '<=', date_to),
             ('team_id', 'in', team_ids if team_ids else [False])],
            ['team_id', 'amount_total:sum'],
            ['team_id']
        )
        
        return {
            'doc_ids': docids,
            'date_from': date_from,
            'date_to': date_to,
            'sales_data': sales_data,
        }
```

**Wizard to collect parameters:**
```python
class SalesReportWizard(models.TransientModel):
    _name = 'sales.report.wizard'
    
    date_from = fields.Date(required=True)
    date_to = fields.Date(required=True)
    team_ids = fields.Many2many('crm.team', string='Teams')
    
    def action_generate_report(self):
        """Generate report with parameters"""
        return {
            'type': 'ir.actions.report',
            'report_name': 'module_name.report_sales_analysis',
            'data': {
                'date_from': self.date_from,
                'date_to': self.date_to,
                'team_ids': self.team_ids.ids,
            },
        }
```

**Template receives data:**
```xml
<template id="report_sales_analysis">
    <t t-call="web.html_container">
        <t t-call="web.external_layout">
            <div class="page">
                <h2>Sales Analysis</h2>
                <p>Period: <t t-esc="date_from"/> to <t t-esc="date_to"/></p>
                
                <table class="table">
                    <tr t-foreach="sales_data" t-as="row">
                        <td t-esc="row['team_id'][1]"/>
                        <td t-esc="row['amount_total']"/>
                    </tr>
                </table>
            </div>
        </t t-call>
    </t t-call>
</template>
```

---

### **8. EXPORT FORMATS (PDF, HTML, Excel)**

```python
class AccountMove(models.Model):
    _name = 'account.move'
    
    def action_print_invoice_pdf(self):
        """Export as PDF"""
        return self.env.ref('account.action_move_out_invoice_0').report_action(self)
    
    def action_print_invoice_html(self):
        """Export as HTML"""
        return {
            'type': 'ir.actions.report',
            'report_name': 'account.report_invoice',
            'report_type': 'qweb-html',  # HTML instead of PDF
            'docids': self.ids,
        }
    
    def action_export_invoice_excel(self):
        """Export to Excel (See Skill #10 for XLSX module)"""
        import base64
        from io import BytesIO
        
        # Use openpyxl to create Excel
        wb = openpyxl.Workbook()
        ws = wb.active
        
        # Add headers
        ws['A1'] = 'Invoice'
        ws['B1'] = 'Partner'
        ws['C1'] = 'Amount'
        
        # Add data
        for idx, line in enumerate(self.invoice_line_ids, start=2):
            ws[f'A{idx}'] = self.name
            ws[f'B{idx}'] = self.partner_id.name
            ws[f'C{idx}'] = line.price_total
        
        # Save to BytesIO
        output = BytesIO()
        wb.save(output)
        
        # Return as download
        attachment = self.env['ir.attachment'].create({
            'name': f'{self.name}.xlsx',
            'type': 'binary',
            'datas': base64.b64encode(output.getvalue()),
        })
        
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }
```

---

### **9. PERFORMANCE OPTIMIZATION FOR REPORTS**

```python
# BAD: N+1 queries in template
class ReportInvoice(models.AbstractModel):
    _name = 'report.account.report_invoice'
    
    @api.model
    def _get_report_values(self, docids, data=None):
        # Template will query partner for EACH line
        # Total queries = lines count + 1
        return {
            'doc_ids': docids,
            'doc_model': 'account.move',
            'docs': self.env['account.move'].browse(docids),
        }

# GOOD: Pre-fetch all data
class ReportInvoice(models.AbstractModel):
    _name = 'report.account.report_invoice'
    
    @api.model
    def _get_report_values(self, docids, data=None):
        invoices = self.env['account.move'].browse(docids)
        
        # Pre-load related data
        invoices.partner_id  # Force load all partners at once
        invoices.invoice_line_ids  # Force load all lines
        
        # Fetch only needed fields
        lines_data = {}
        for invoice in invoices:
            lines_data[invoice.id] = invoice.invoice_line_ids.read([
                'product_id', 'quantity', 'price_unit', 'price_total'
            ])
        
        return {
            'doc_ids': docids,
            'doc_model': 'account.move',
            'docs': invoices,
            'lines_data': lines_data,  # Pre-computed
        }
```

**Template uses pre-computed data:**
```xml
<template id="report_invoice">
    <t t-foreach="docs" t-as="doc">
        <div class="page">
            <table>
                <tr t-foreach="lines_data[doc.id]" t-as="line">
                    <td t-esc="line['product_id'][1]"/>
                    <td t-esc="line['quantity']"/>
                </tr>
            </table>
        </div>
    </t>
</template>
```

---

### **10. ADVANCED: SUBREPORTS & CONDITIONAL SECTIONS**

```xml
<!-- Main report with subreport -->
<template id="report_sale_order">
    <t t-call="web.html_container">
        <t t-call="web.external_layout">
            <div class="page">
                <h1 t-field="doc.name"/>
                
                <!-- Include subreport for customer info -->
                <t t-call="module_name.report_subreport_customer"/>
                
                <!-- Conditional section -->
                <t t-if="doc.amount_total > 10000">
                    <div class="alert alert-info">
                        <strong>Large Order:</strong> This order exceeds $10,000
                    </div>
                </t>
                
                <!-- Include subreport for line items -->
                <t t-call="module_name.report_subreport_lines"/>
                
                <!-- Page break before notes -->
                <div class="page-break"/>
                
                <!-- Include subreport for notes -->
                <t t-call="module_name.report_subreport_notes"/>
            </div>
        </t t-call>
    </t t-call>
</template>

<!-- Subreport: Customer Info -->
<template id="report_subreport_customer">
    <div class="customer-info">
        <h3>Customer Information</h3>
        <p><strong>Name:</strong> <t t-field="doc.partner_id.name"/></p>
        <p><strong>Address:</strong> <t t-field="doc.partner_id.street"/></p>
        <p><strong>City:</strong> <t t-field="doc.partner_id.city"/></p>
    </div>
</template>

<!-- Subreport: Line Items -->
<template id="report_subreport_lines">
    <div class="line-items">
        <h3>Order Items</h3>
        <table class="table">
            <thead>
                <tr>
                    <th>Product</th>
                    <th>Qty</th>
                    <th>Price</th>
                    <th>Total</th>
                </tr>
            </thead>
            <tbody>
                <tr t-foreach="doc.order_line" t-as="line">
                    <td t-esc="line.product_id.name"/>
                    <td t-esc="line.product_uom_qty"/>
                    <td t-esc="line.price_unit"/>
                    <td t-esc="line.price_total"/>
                </tr>
            </tbody>
        </table>
    </div>
</template>

<!-- Subreport: Notes -->
<template id="report_subreport_notes">
    <div class="notes">
        <h3>Order Notes</h3>
        <p t-field="doc.note" t-options="{'widget': 'html'}"/>
    </div>
</template>
```

---

## 🔗 **INTEGRATION WITH SKILL #9 (SQL QUERIES)**

### **Use Case 1: Complex AR Aging Report**

**Fetch data with SQL (from Skill #9):**
```python
def get_ar_aging(self):
    """Complex aging analysis"""
    self.env.cr.execute("""
        SELECT 
            partner_id,
            SUM(CASE WHEN age <= 30 THEN amount_residual ELSE 0 END) as current,
            SUM(CASE WHEN age > 30 AND age <= 60 THEN amount_residual ELSE 0 END) as past_30,
            SUM(CASE WHEN age > 60 AND age <= 90 THEN amount_residual ELSE 0 END) as past_60,
            SUM(CASE WHEN age > 90 THEN amount_residual ELSE 0 END) as past_90
        FROM (
            SELECT 
                partner_id,
                amount_residual,
                EXTRACT(DAY FROM CURRENT_DATE - date)::int as age
            FROM account_move
            WHERE move_type = 'out_invoice'
            AND state = 'posted'
            AND amount_residual > 0
        ) aging
        GROUP BY partner_id
        ORDER BY partner_id
    """)
    
    return self.env.cr.dictfetchall()
```

**Render in QWeb:**
```xml
<template id="report_ar_aging">
    <t t-call="web.html_container">
        <t t-call="web.external_layout">
            <div class="page">
                <h2>Accounts Receivable Aging</h2>
                <table class="table">
                    <thead>
                        <tr>
                            <th>Partner</th>
                            <th>Current</th>
                            <th>30-60 Days</th>
                            <th>60-90 Days</th>
                            <th>90+ Days</th>
                            <th>Total</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr t-foreach="doc.get_ar_aging()" t-as="row">
                            <td t-esc="row['partner_id']"/>
                            <td t-esc="row['current']" t-options="{'widget': 'monetary'}"/>
                            <td t-esc="row['past_30']" t-options="{'widget': 'monetary'}"/>
                            <td t-esc="row['past_60']" t-options="{'widget': 'monetary'}"/>
                            <td t-esc="row['past_90']" t-options="{'widget': 'monetary'}"/>
                            <td t-esc="row['current'] + row['past_30'] + row['past_60'] + row['past_90']"/>
                        </tr>
                    </tbody>
                </table>
            </div>
        </t t-call>
    </t t-call>
</template>
```

---

### **Use Case 2: Sales Analysis with read_group (from Skill #9)**

```python
def get_sales_by_team(self, date_from, date_to):
    """Use read_group for fast aggregation (See Skill #9)"""
    return self.env['sale.order'].read_group(
        [('date_order', '>=', date_from),
         ('date_order', '<=', date_to),
         ('state', '!=', 'cancel')],
        ['team_id', 'amount_total:sum', 'amount_tax:sum', 'amount_untaxed:sum'],
        ['team_id'],
        lazy=False
    )

def get_sales_by_month(self, year):
    """Aggregate by month"""
    return self.env['sale.order'].read_group(
        [('date_order', '>=', f'{year}-01-01'),
         ('date_order', '<', f'{year+1}-01-01')],
        ['date_order', 'amount_total:sum'],
        ['date_order:month']
    )
```

**Template:**
```xml
<template id="report_sales_analysis">
    <t t-call="web.html_container">
        <t t-call="web.external_layout">
            <div class="page">
                <h2>Sales Analysis</h2>
                
                <!-- Team Summary -->
                <h3>By Team</h3>
                <table class="table">
                    <tr t-foreach="doc.get_sales_by_team(date_from, date_to)" t-as="row">
                        <td t-esc="row['team_id'][1]"/>
                        <td t-esc="row['amount_total']"/>
                    </tr>
                </table>
                
                <!-- Monthly Summary -->
                <h3>By Month</h3>
                <table class="table">
                    <tr t-foreach="doc.get_sales_by_month(year)" t-as="row">
                        <td t-esc="row['date_order']"/>
                        <td t-esc="row['amount_total']"/>
                    </tr>
                </table>
            </div>
        </t t-call>
    </t t-call>
</template>
```

---

## 📚 **COMMON REPORT TYPES**

### **1. Transactional Reports (Invoice, SO, PO)**
```xml
<!-- Template fetches single document -->
<template id="report_invoice">
    <t t-foreach="docs" t-as="doc">
        <div class="page">
            <h1 t-field="doc.name"/>
            <!-- Details -->
        </div>
    </t>
</template>
```

### **2. Batch Reports (Multiple Documents)**
```xml
<!-- Template loops through docs -->
<template id="report_batch_invoices">
    <t t-foreach="docs" t-as="doc">
        <div class="page">
            <h2 t-field="doc.name"/>
            <div t-field="doc.partner_id.name"/>
        </div>
        <div class="page-break t-if="not loop.last"/>  <!-- Break except last -->
    </t>
</template>
```

### **3. Summary/Analytics Reports**
```xml
<!-- Template fetches aggregated data -->
<template id="report_sales_summary">
    <div class="page">
        <h2>Sales Summary</h2>
        <!-- Aggregated tables from read_group or SQL -->
    </div>
</template>
```

### **4. Statement Reports (AR/AP Aging)**
```xml
<!-- Complex multi-table layout -->
<template id="report_ar_statement">
    <!-- Aging buckets -->
    <!-- Customer payments -->
    <!-- Summary totals -->
</template>
```

---

## ⚡ **PERFORMANCE BEST PRACTICES**

✅ **Do:**
- Use `read_group()` for aggregations (Skill #9)
- Pre-fetch related data before template
- Limit fields in `.read()`
- Use SQL for complex queries (Skill #9)
- Cache computed values

❌ **Don't:**
- Loop through related records in template (N+1)
- Call methods in loops (expensive)
- Fetch unnecessary fields
- Compute in template (do in Python)
- Use raw `search()` without filtering

---

## 🎯 **REPORT CHECKLIST**

- ✅ Define `ir.actions.report`
- ✅ Create QWeb template
- ✅ Add styling (CSS)
- ✅ Fetch data efficiently (Skill #9)
- ✅ Handle pagination (page breaks)
- ✅ Test PDF generation
- ✅ Test HTML preview
- ✅ Optimize queries
- ✅ Add conditional sections
- ✅ Format currency/dates

---

**Skill Status:** ✅ Ready for all Report Development tasks

