---
name: odoo-qweb-reports-development
description: Odoo QWeb Report Development (v16-19) covering QWeb templating, data fetching (read_group, SQL), report controllers, styling, pagination, and performance. Generic patterns for all report types (Invoice, PO, SO, etc).
---

# Odoo QWeb Reports Development

## Report Registration

**Define Report Action**
```xml
<!-- reports/report_action.xml -->
<record id="action_report_custom" model="ir.actions.report">
  <field name="name">Custom Report</field>
  <field name="model">sale.order</field>
  <field name="report_type">qweb-pdf</field>
  <field name="report_name">module_name.report_custom</field>
  <field name="report_file">module_name.report_custom</field>
  <field name="binding_model_id" ref="sale.model_sale_order"/>
  <field name="binding_type">report</field>
</record>

<!-- HTML Preview (Optional) -->
<record id="action_report_custom_html" model="ir.actions.report">
  <field name="name">Custom Report (HTML)</field>
  <field name="model">sale.order</field>
  <field name="report_type">qweb-html</field>
  <field name="report_name">module_name.report_custom</field>
</record>
```

**Key Fields:**
- `report_type`: `qweb-pdf`, `qweb-html`, `qweb-text`
- `report_name`: Dot notation (module.template_id)
- `binding_model_id`: Link to model
- `binding_type`: `report` or `action`

## QWeb Template Basics

**Minimal Template**
```xml
<template id="report_custom">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <div class="page">
        <!-- Content here -->
      </div>
    </t>
  </t>
</template>
```

**Template Variables:**
- `doc` - Current record
- `docs` - Multiple records (loop)
- `company` - Current company
- `user` - Current user
- `context_timestamp()` - Timezone-aware timestamps

## QWeb Directives & Syntax

**Data Access**
```xml
<!-- Field value -->
<p t-field="doc.name"/>

<!-- Escaped (HTML safe) -->
<p t-esc="doc.amount_total"/>

<!-- Raw HTML -->
<p t-raw="doc.description"/>

<!-- Set variable -->
<t t-set="variable_name" t-value="doc.field"/>

<!-- Computed variable -->
<t t-set="subtotal" t-value="sum(line.price_subtotal for line in doc.lines)"/>
```

**Conditions**
```xml
<t t-if="doc.state == 'draft'">
  <span>DRAFT</span>
</t>

<t t-elif="doc.state == 'done'">
  <span>COMPLETED</span>
</t>

<t t-else="">
  <span>OTHER</span>
</t>
```

**Loops**
```xml
<!-- Loop through records -->
<t t-foreach="doc.line_ids" t-as="line">
  <tr>
    <td t-esc="line.product_id.name"/>
    <td t-esc="line_index"/>            <!-- Current index -->
    <td t-if="line_first">First Item</td>
    <td t-if="line_last">Last Item</td>
  </tr>
</t>
```

**Formatting**
```xml
<!-- Currency -->
<p t-field="doc.amount" t-options="{'widget': 'monetary', 'display_currency': company.currency_id}"/>

<!-- Date -->
<p t-field="doc.date_order" t-options="{'widget': 'date'}"/>
<p t-field="doc.date_order" t-options="{'format': 'dd/MM/yyyy'}"/>

<!-- Percentage -->
<p t-esc="'{:.1f}%'.format(value * 100)"/>

<!-- Thousand separator -->
<p t-esc="'{:,.2f}'.format(doc.amount_total)"/>
```

**String Operations**
```xml
<!-- Slice -->
<p t-esc="doc.name[:10]"/>

<!-- Format -->
<p t-esc="'Item: %s' % doc.product_id.name"/>

<!-- Methods -->
<p t-esc="doc.partner_id.name.upper()"/>

<!-- Conditional class -->
<div t-attf-class="alert alert-#{doc.state == 'done' and 'success' or 'danger'}">
  Status: <t t-esc="doc.state"/>
</div>
```

**Calling Python Functions**
```xml
<!-- Call method -->
<p t-esc="doc.get_report_data()"/>

<!-- Call with parameters -->
<p t-esc="doc.calculate_discount(10)"/>
```

**Comments**
```xml
<!-- Single line comment (not rendered) -->

<t t-comment>
  Multi-line comment
  Not rendered in PDF
</t>
```

## Data Fetching Strategies

### **Option 1: Direct Field Access (Simple)**
```python
# In model
class SaleOrder(models.Model):
    _inherit = 'sale.order'
    
    @property
    def report_data(self):
        """Return formatted report data"""
        return {
            'total_items': len(self.order_line),
            'total_quantity': sum(self.order_line.mapped('product_uom_qty')),
            'subtotal': self.amount_untaxed,
        }
```

**In template:**
```xml
<p t-esc="doc.order_line[0].product_id.name"/>
<p t-esc="doc.amount_total"/>
```

### **Option 2: Pre-computed with read() (Fast)**
```python
def get_report_lines(self):
    """Fetch only needed fields (fast)"""
    return self.order_line.read([
        'product_id', 'quantity', 'price_unit', 'price_subtotal'
    ])
```

**In template:**
```xml
<t t-set="lines" t-value="doc.get_report_lines()"/>
<t t-foreach="lines" t-as="line">
  <tr>
    <td t-esc="line['product_id'][1]"/>
    <td t-esc="line['quantity']"/>
    <td t-esc="line['price_subtotal']"/>
  </tr>
</t>
```

### **Option 3: read_group() for Aggregation (Efficient)**
```python
def get_sales_summary(self, date_from, date_to):
    """Aggregate data by team"""
    return self.env['sale.order'].read_group(
        [('date_order', '>=', date_from),
         ('date_order', '<=', date_to),
         ('state', '!=', 'cancel')],
        ['team_id', 'amount_total:sum', 'order_line_count:sum'],
        ['team_id'],
        lazy=False
    )
```

**In template:**
```xml
<table>
  <t t-foreach="doc.get_sales_summary(date_from, date_to)" t-as="row">
    <tr>
      <td t-esc="row['team_id'][1]"/>
      <td t-esc="'{:,.2f}'.format(row['amount_total'])"/>
    </tr>
  </t>
</table>
```

### **Option 4: SQL Query (Complex)**
```python
def get_complex_report_data(self, filters):
    """Complex SQL query for performance"""
    self.env.cr.execute("""
        SELECT
            p.name as product_name,
            SUM(sol.product_uom_qty) as total_qty,
            SUM(sol.price_subtotal) as total_amount,
            COUNT(DISTINCT so.id) as order_count
        FROM sale_order_line sol
        INNER JOIN sale_order so ON sol.order_id = so.id
        INNER JOIN product_product p ON sol.product_id = p.id
        WHERE so.date_order >= %s
        AND so.date_order <= %s
        AND so.state != 'cancel'
        GROUP BY p.id, p.name
        ORDER BY total_amount DESC
    """, (filters['date_from'], filters['date_to']))
    
    columns = ['product_name', 'total_qty', 'total_amount', 'order_count']
    return [dict(zip(columns, row)) for row in self.env.cr.fetchall()]
```

**In template:**
```xml
<table>
  <t t-foreach="doc.get_complex_report_data(filters)" t-as="row">
    <tr>
      <td t-esc="row['product_name']"/>
      <td t-esc="row['total_qty']"/>
      <td t-esc="'{:,.2f}'.format(row['total_amount'])"/>
    </tr>
  </t>
</table>
```

## Report Controller (Custom Routes)

```python
# controllers/report.py
from odoo import http
from odoo.http import request

class ReportController(http.Controller):
    
    @http.route('/report/<string:report_name>/<int:record_id>', 
                 type='http', auth='user', website=True)
    def report_pdf_custom(self, report_name, record_id):
        """Custom PDF generation with additional data"""
        record = request.env['sale.order'].browse(record_id)
        
        # Add extra context
        extra_data = {
            'custom_discount': 10,
            'company_tagline': request.env.company.name,
        }
        
        return request.make_response(
            request.env['ir.actions.report'].sudo()._render(
                report_name, 
                [record.id], 
                data=extra_data
            ),
            headers=[('Content-Type', 'application/pdf')]
        )
    
    @http.route('/report/preview/<string:report_name>/<int:record_id>',
                 type='http', auth='user', website=True)
    def report_preview(self, report_name, record_id):
        """HTML preview"""
        record = request.env['sale.order'].browse(record_id)
        return request.render(f'{report_name}_preview', {
            'doc': record,
            'company': request.env.company,
        })
```

## Report Styling (CSS in QWeb)

**External Layout with Custom CSS**
```xml
<template id="report_custom_layout" inherit_id="web.external_layout">
  <xpath expr="//head" position="inside">
    <style>
      .report-header {
        border-bottom: 2px solid #333;
        padding-bottom: 10px;
        margin-bottom: 20px;
      }
      
      .report-table {
        width: 100%;
        border-collapse: collapse;
      }
      
      .report-table th {
        background-color: #f0f0f0;
        font-weight: bold;
        border: 1px solid #ddd;
        padding: 8px;
      }
      
      .report-table td {
        border: 1px solid #ddd;
        padding: 8px;
      }
      
      .report-table tr:nth-child(even) {
        background-color: #f9f9f9;
      }
      
      .text-right {
        text-align: right;
      }
      
      .bg-light {
        background-color: #f8f9fa;
      }
      
      .border-top {
        border-top: 1px solid #dee2e6 !important;
      }
      
      .page-break {
        page-break-after: always;
      }
    </style>
  </xpath>
</template>
```

## Pagination & Multiple Pages

**Page Break Between Documents**
```xml
<template id="report_multiple_docs">
  <t t-foreach="docs" t-as="doc">
    <div class="page">
      <h1 t-field="doc.name"/>
      <!-- Content -->
    </div>
    <!-- Page break except last doc -->
    <div t-if="not loop.last" class="page-break"/>
  </t>
</template>
```

**Page Break Within Document**
```xml
<template id="report_long_document">
  <div class="page">
    <h2>Section 1</h2>
    <p>Content...</p>
  </div>
  
  <div class="page">
    <h2>Section 2</h2>
    <p>Content...</p>
  </div>
</template>
```

## Report Inheritance

**Override Existing Report**
```xml
<template id="report_sale_order" inherit_id="sale.report_saleorder">
  <!-- Override header -->
  <xpath expr="//div[@class='address']" position="replace">
    <div class="address">
      <p><strong>Custom Header</strong></p>
    </div>
  </xpath>
  
  <!-- Add new section -->
  <xpath expr="//table[@class='table table-sm']" position="after">
    <div class="custom-section">
      <h3>Additional Information</h3>
    </div>
  </xpath>
</template>
```

## Common Report Patterns

| Use Case | Pattern | Code |
|----------|---------|------|
| Single Doc | Loop through docs | `<t t-foreach="docs" t-as="doc">` |
| Multi-page | Page break per doc | `<div class="page">` each doc |
| Aggregation | read_group | `read_group(..., lazy=False)` |
| Complex Query | SQL + loop | `env.cr.execute(...)` |
| Formatting | Filters | `t-options="{'widget': 'date'}"` |
| Conditional | If/elif/else | `<t t-if="condition">` |

## Performance Best Practices

✅ **Do:**
- Use `read_group()` for aggregations
- Pre-fetch with `.read()` limiting fields
- Use SQL for complex queries
- Cache computed values in Python
- Minimize ORM calls in loops

❌ **Don't:**
- Access related records in loops (N+1)
- Call methods repeatedly in template
- Use `search()` without filtering
- Fetch unnecessary fields
- Compute data in template

## Version Notes (v16-19)

| Feature | v16 | v17+ | v18+ | v19+ |
|---------|-----|------|------|------|
| QWeb Templates | Full | Full | Full | Full |
| PDF Renderer | wkhtmltopdf | wkhtmltopdf | Chrome | Chrome |
| SQL Queries | Manual | Manual | Optimized | Optimized |
| Report Filters | Basic | Enhanced | Advanced | Advanced |
| read_group() | Yes | Enhanced | Enhanced | Enhanced |

## Report Checklist

```
✓ Report action registered (ir.actions.report)
✓ QWeb template created with external_layout
✓ Python data methods written
✓ SQL queries optimized
✓ Date/currency formatting applied
✓ Conditional sections working
✓ Page breaks configured
✓ PDF generation tested
✓ HTML preview working
✓ Multi-document handling verified
✓ Performance optimized
✓ Styling applied
```

## Quick Template Skeleton

```xml
<template id="report_custom">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <style>
        /* Styling here */
      </style>
      
      <t t-foreach="docs" t-as="doc">
        <div class="page">
          <!-- Header -->
          <div class="row mb-4">
            <div class="col-6">
              <h2>Report Title</h2>
              <p t-field="doc.name"/>
            </div>
            <div class="col-6 text-right">
              <img t-if="company.logo" src="..."/>
            </div>
          </div>
          
          <!-- Content -->
          <table class="table">
            <thead>
              <tr>
                <th>Column 1</th>
                <th>Column 2</th>
              </tr>
            </thead>
            <tbody>
              <t t-foreach="doc.lines" t-as="line">
                <tr>
                  <td t-esc="line.field1"/>
                  <td t-esc="line.field2"/>
                </tr>
              </t>
            </tbody>
          </table>
          
          <!-- Totals -->
          <table class="table">
            <tr>
              <td><strong>Total</strong></td>
              <td t-esc="'{:,.2f}'.format(doc.amount_total)"/>
            </tr>
          </table>
        </div>
        
        <!-- Page break -->
        <div t-if="not loop.last" class="page-break"/>
      </t>
    </t>
  </t>
</template>
```
