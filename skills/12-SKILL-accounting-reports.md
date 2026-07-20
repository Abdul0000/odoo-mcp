---
name: odoo-accounting-reports
description: Odoo accounting reports (v16-19) covering Invoice QWeb, AR Aging, P&L, Balance Sheet, Tax Reports, payment statements (Pakistan GST/withholding). Short, powerful QWeb reference for accounting.
---

# Odoo Accounting Reports (QWeb)

## Invoice Report (QWeb)

**Register Report**
```xml
<!-- reports/invoice_report.xml -->
<record id="action_report_invoice_custom" model="ir.actions.report">
  <field name="name">Invoice</field>
  <field name="model">account.move</field>
  <field name="report_type">qweb-pdf</field>
  <field name="report_name">account_custom.report_invoice</field>
  <field name="binding_model_id" ref="account.model_account_move"/>
  <field name="binding_type">report</field>
</record>

<template id="report_invoice">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <div class="page">
        <!-- Header -->
        <div class="row mb-5">
          <div class="col-6">
            <h2>INVOICE</h2>
            <p><strong>Invoice #:</strong> <t t-esc="doc.name"/></p>
            <p><strong>Date:</strong> <t t-field="doc.invoice_date" t-options="{'widget': 'date'}"/></p>
          </div>
          <div class="col-6 text-right">
            <img t-if="company.logo" t-attf-src="data:image/png;base64,{company.logo}" style="max-height: 60px;"/>
          </div>
        </div>

        <!-- Customer Info -->
        <div class="row mb-4">
          <div class="col-6">
            <h4>Bill To:</h4>
            <p><strong><t t-esc="doc.partner_id.name"/></strong></p>
            <p t-if="doc.partner_id.street"><t t-esc="doc.partner_id.street"/></p>
            <p t-if="doc.partner_id.city"><t t-esc="doc.partner_id.city"/>, <t t-esc="doc.partner_id.state_id.name"/></p>
            <p t-if="doc.partner_id.vat"><strong>Tax ID:</strong> <t t-esc="doc.partner_id.vat"/></p>
          </div>
          <div class="col-6">
            <h4>Payment Terms:</h4>
            <p><strong>Due Date:</strong> <t t-field="doc.invoice_date_due" t-options="{'widget': 'date'}"/></p>
            <p><strong>Payment Terms:</strong> <t t-esc="doc.payment_term_id.name or 'Net 30'"/></p>
          </div>
        </div>

        <!-- Line Items -->
        <div class="mb-4">
          <table class="table table-sm">
            <thead>
              <tr>
                <th>Description</th>
                <th class="text-right">Qty</th>
                <th class="text-right">Unit Price</th>
                <th class="text-right">Amount</th>
              </tr>
            </thead>
            <tbody>
              <t t-foreach="doc.invoice_line_ids" t-as="line">
                <tr>
                  <td><t t-esc="line.product_id.name or line.name"/></td>
                  <td class="text-right"><t t-esc="line.quantity"/></td>
                  <td class="text-right"><t t-esc="'{:.2f}'.format(line.price_unit)"/></td>
                  <td class="text-right"><t t-esc="'{:.2f}'.format(line.price_subtotal)"/></td>
                </tr>
              </t>
            </tbody>
          </table>
        </div>

        <!-- Totals -->
        <div class="row">
          <div class="col-6"/>
          <div class="col-6">
            <table class="table table-sm">
              <tr>
                <td><strong>Subtotal:</strong></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(doc.amount_untaxed)"/></td>
              </tr>
              <t t-foreach="doc.invoice_line_ids.mapped('tax_ids')" t-as="tax">
                <tr t-if="tax not in doc.invoice_line_ids[0].tax_ids or tax_index == 0">
                  <td><strong><t t-esc="tax.name"/>:</strong></td>
                  <td class="text-right">
                    <t t-set="tax_amount" t-value="sum(line.price_subtotal * tax.amount / 100 for line in doc.invoice_line_ids.filtered(lambda l: tax in l.tax_ids))"/>
                    <t t-esc="'{:.2f}'.format(tax_amount)"/>
                  </td>
                </tr>
              </t>
              <tr class="border-top border-dark">
                <td><strong>Total:</strong></td>
                <td class="text-right"><strong><t t-esc="'{:.2f}'.format(doc.amount_total)"/></strong></td>
              </tr>
            </table>
          </div>
        </div>

        <!-- Notes -->
        <div t-if="doc.narration" class="mt-4">
          <h5>Notes:</h5>
          <p t-esc="doc.narration"/>
        </div>

        <!-- Footer -->
        <div class="mt-5 text-center text-muted">
          <p>Thank you for your business!</p>
          <p t-esc="company.name"/> | <t t-esc="company.email"/> | <t t-esc="company.phone"/>
        </div>
      </div>
    </t>
  </t>
</template>
```

## Aged Receivable Report (QWeb)

**Register Report**
```xml
<record id="action_report_ar_aging" model="ir.actions.report">
  <field name="name">Aged Receivable</field>
  <field name="model">account.move</field>
  <field name="report_type">qweb-pdf</field>
  <field name="report_name">account_custom.report_ar_aging</field>
</record>

<template id="report_ar_aging">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <div class="page">
        <h2>Accounts Receivable Aging</h2>
        <p>As of <t t-esc="context_timestamp(datetime.datetime.now()).strftime('%d/%m/%Y')"/></p>

        <table class="table table-bordered">
          <thead>
            <tr class="bg-light">
              <th>Customer</th>
              <th class="text-right">Invoice #</th>
              <th class="text-right">Invoice Date</th>
              <th class="text-right">Amount</th>
              <th class="text-right">1-30 Days</th>
              <th class="text-right">31-60 Days</th>
              <th class="text-right">61-90 Days</th>
              <th class="text-right">90+ Days</th>
            </tr>
          </thead>
          <tbody>
            <t t-set="current_partner" t-value="None"/>
            <t t-foreach="get_aged_receivable()" t-as="line">
              <t t-if="current_partner != line['partner_id']">
                <tr class="bg-light font-weight-bold">
                  <td colspan="8"><t t-esc="line['partner_name']"/></td>
                </tr>
                <t t-set="current_partner" t-value="line['partner_id']"/>
              </t>
              <tr>
                <td/>
                <td class="text-right"><t t-esc="line['invoice_number']"/></td>
                <td class="text-right"><t t-esc="line['invoice_date']"/></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(line['amount_total'])"/></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(line['1_30'])"/></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(line['31_60'])"/></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(line['61_90'])"/></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(line['90_plus'])"/></td>
              </tr>
            </t>
          </tbody>
        </table>
      </div>
    </t>
  </t>
</template>
```

**Python Method (Data Fetching)**
```python
def get_aged_receivable(self):
    """Fetch aged receivable data"""
    self.env.cr.execute("""
        SELECT
            p.id as partner_id,
            p.name as partner_name,
            am.name as invoice_number,
            am.date as invoice_date,
            am.amount_total,
            CASE 
                WHEN EXTRACT(DAY FROM CURRENT_DATE - am.date) <= 30 
                THEN am.amount_residual ELSE 0 
            END as "1_30",
            CASE 
                WHEN EXTRACT(DAY FROM CURRENT_DATE - am.date) > 30 
                AND EXTRACT(DAY FROM CURRENT_DATE - am.date) <= 60 
                THEN am.amount_residual ELSE 0 
            END as "31_60",
            CASE 
                WHEN EXTRACT(DAY FROM CURRENT_DATE - am.date) > 60 
                AND EXTRACT(DAY FROM CURRENT_DATE - am.date) <= 90 
                THEN am.amount_residual ELSE 0 
            END as "61_90",
            CASE 
                WHEN EXTRACT(DAY FROM CURRENT_DATE - am.date) > 90 
                THEN am.amount_residual ELSE 0 
            END as "90_plus"
        FROM account_move am
        INNER JOIN res_partner p ON am.partner_id = p.id
        WHERE am.move_type = 'out_invoice'
        AND am.state = 'posted'
        AND am.amount_residual > 0
        ORDER BY p.name, am.date
    """)
    return self.env.cr.dictfetchall()
```

## Profit & Loss Report (QWeb)

**Register Report**
```xml
<record id="action_report_pl" model="ir.actions.report">
  <field name="name">Profit & Loss</field>
  <field name="model">account.move</field>
  <field name="report_type">qweb-pdf</field>
  <field name="report_name">account_custom.report_pl</field>
</record>

<template id="report_pl">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <div class="page">
        <h2>Profit & Loss Statement</h2>
        <p>For Period: <t t-esc="start_date"/> to <t t-esc="end_date"/></p>

        <table class="table">
          <tr>
            <th>Account</th>
            <th class="text-right">Amount (PKR)</th>
          </tr>

          <!-- Revenue Section -->
          <tr class="font-weight-bold bg-light">
            <td colspan="2">REVENUE</td>
          </tr>
          <t t-foreach="get_pl_section('revenue', start_date, end_date)" t-as="line">
            <tr>
              <td t-esc="line['account_name']"/>
              <td class="text-right"><t t-esc="'{:,.2f}'.format(line['amount'])"/></td>
            </tr>
          </t>
          <tr class="border-top border-dark">
            <td><strong>Total Revenue</strong></td>
            <td class="text-right"><strong><t t-esc="'{:,.2f}'.format(get_pl_total('revenue', start_date, end_date))"/></strong></td>
          </tr>

          <!-- COGS Section -->
          <tr class="font-weight-bold bg-light">
            <td colspan="2">COST OF GOODS SOLD</td>
          </tr>
          <t t-foreach="get_pl_section('cogs', start_date, end_date)" t-as="line">
            <tr>
              <td t-esc="line['account_name']"/>
              <td class="text-right"><t t-esc="'{:,.2f}'.format(line['amount'])"/></td>
            </tr>
          </t>
          <tr>
            <td><strong>Gross Profit</strong></td>
            <td class="text-right"><strong><t t-esc="'{:,.2f}'.format(get_pl_total('revenue', start_date, end_date) - get_pl_total('cogs', start_date, end_date))"/></strong></td>
          </tr>

          <!-- Operating Expenses -->
          <tr class="font-weight-bold bg-light">
            <td colspan="2">OPERATING EXPENSES</td>
          </tr>
          <t t-foreach="get_pl_section('expenses', start_date, end_date)" t-as="line">
            <tr>
              <td t-esc="line['account_name']"/>
              <td class="text-right"><t t-esc="'{:,.2f}'.format(line['amount'])"/></td>
            </tr>
          </t>
          <tr class="border-top border-dark">
            <td><strong>NET PROFIT</strong></td>
            <td class="text-right"><strong><t t-esc="'{:,.2f}'.format(get_net_profit(start_date, end_date))"/></strong></td>
          </tr>
        </table>
      </div>
    </t>
  </t>
</template>
```

## Balance Sheet Report (QWeb)

```xml
<record id="action_report_balance_sheet" model="ir.actions.report">
  <field name="name">Balance Sheet</field>
  <field name="model">account.move</field>
  <field name="report_type">qweb-pdf</field>
  <field name="report_name">account_custom.report_balance_sheet</field>
</record>

<template id="report_balance_sheet">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <div class="page">
        <h2>Balance Sheet</h2>
        <p>As of <t t-esc="as_of_date"/></p>

        <div class="row">
          <!-- Assets -->
          <div class="col-6">
            <h4>ASSETS</h4>
            <table class="table table-sm">
              <t t-foreach="get_balance_sheet_accounts('asset', as_of_date)" t-as="account">
                <tr>
                  <td><t t-esc="account['account_name']"/></td>
                  <td class="text-right"><t t-esc="'{:,.2f}'.format(account['balance'])"/></td>
                </tr>
              </t>
              <tr class="border-top border-dark font-weight-bold">
                <td>Total Assets</td>
                <td class="text-right"><t t-esc="'{:,.2f}'.format(get_balance_sheet_total('asset', as_of_date))"/></td>
              </tr>
            </table>
          </div>

          <!-- Liabilities & Equity -->
          <div class="col-6">
            <h4>LIABILITIES & EQUITY</h4>
            <table class="table table-sm">
              <tr class="bg-light font-weight-bold">
                <td colspan="2">LIABILITIES</td>
              </tr>
              <t t-foreach="get_balance_sheet_accounts('liability', as_of_date)" t-as="account">
                <tr>
                  <td><t t-esc="account['account_name']"/></td>
                  <td class="text-right"><t t-esc="'{:,.2f}'.format(account['balance'])"/></td>
                </tr>
              </t>
              <tr>
                <td><strong>Total Liabilities</strong></td>
                <td class="text-right"><strong><t t-esc="'{:,.2f}'.format(get_balance_sheet_total('liability', as_of_date))"/></strong></td>
              </tr>

              <tr class="bg-light font-weight-bold">
                <td colspan="2">EQUITY</td>
              </tr>
              <t t-foreach="get_balance_sheet_accounts('equity', as_of_date)" t-as="account">
                <tr>
                  <td><t t-esc="account['account_name']"/></td>
                  <td class="text-right"><t t-esc="'{:,.2f}'.format(account['balance'])"/></td>
                </tr>
              </t>
              <tr class="border-top border-dark font-weight-bold">
                <td>Total Liabilities & Equity</td>
                <td class="text-right"><t t-esc="'{:,.2f}'.format(get_balance_sheet_total('liability', as_of_date) + get_balance_sheet_total('equity', as_of_date))"/></td>
              </tr>
            </table>
          </div>
        </div>
      </div>
    </t>
  </t>
</template>
```

## Tax Compliance Report (Pakistan)

```xml
<record id="action_report_tax_summary" model="ir.actions.report">
  <field name="name">Tax Summary (FBR)</field>
  <field name="model">account.move</field>
  <field name="report_type">qweb-pdf</field>
  <field name="report_name">account_custom.report_tax_summary</field>
</record>

<template id="report_tax_summary">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <div class="page">
        <h2>Tax Compliance Report</h2>
        <p>Period: <t t-esc="start_date"/> to <t t-esc="end_date"/></p>

        <h4>Sales Tax / VAT</h4>
        <table class="table">
          <tr>
            <th>Tax Type</th>
            <th class="text-right">Taxable Base</th>
            <th class="text-right">Tax Rate</th>
            <th class="text-right">Tax Amount</th>
          </tr>
          <t t-foreach="get_tax_summary('sales_tax', start_date, end_date)" t-as="tax">
            <tr>
              <td><t t-esc="tax['tax_name']"/></td>
              <td class="text-right"><t t-esc="'{:,.2f}'.format(tax['base_amount'])"/></td>
              <td class="text-right"><t t-esc="tax['rate']"/>%</td>
              <td class="text-right"><t t-esc="'{:,.2f}'.format(tax['tax_amount'])"/></td>
            </tr>
          </t>
        </table>

        <h4>Withholding Tax</h4>
        <table class="table">
          <tr>
            <th>Vendor</th>
            <th class="text-right">Invoice Amount</th>
            <th class="text-right">Withholding %</th>
            <th class="text-right">Withholding Amount</th>
          </tr>
          <t t-foreach="get_withholding_tax(start_date, end_date)" t-as="wht">
            <tr>
              <td><t t-esc="wht['vendor_name']"/></td>
              <td class="text-right"><t t-esc="'{:,.2f}'.format(wht['invoice_amount'])"/></td>
              <td class="text-right"><t t-esc="wht['rate']"/>%</td>
              <td class="text-right"><t t-esc="'{:,.2f}'.format(wht['wht_amount'])"/></td>
            </tr>
          </t>
        </table>

        <h4 class="mt-4">Summary</h4>
        <table class="table">
          <tr>
            <td>Sales Tax Collected</td>
            <td class="text-right"><t t-esc="'{:,.2f}'.format(get_tax_total('sales_tax', start_date, end_date))"/></td>
          </tr>
          <tr>
            <td>Withholding Tax Deducted</td>
            <td class="text-right"><t t-esc="'{:,.2f}'.format(get_wht_total(start_date, end_date))"/></td>
          </tr>
          <tr class="border-top border-dark font-weight-bold">
            <td>Net Tax Payable</td>
            <td class="text-right"><t t-esc="'{:,.2f}'.format(get_tax_total('sales_tax', start_date, end_date) - get_wht_total(start_date, end_date))"/></td>
          </tr>
        </table>
      </div>
    </t>
  </t>
</template>
```

## Payment Statement

```xml
<template id="report_payment_statement">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <div class="page">
        <h2>Payment Statement</h2>
        <p><strong>Vendor:</strong> <t t-esc="doc.partner_id.name"/></p>
        <p><strong>Period:</strong> <t t-esc="start_date"/> to <t t-esc="end_date"/></p>

        <table class="table table-sm">
          <thead>
            <tr>
              <th>Date</th>
              <th>Invoice #</th>
              <th>Description</th>
              <th class="text-right">Amount</th>
              <th class="text-right">Paid</th>
              <th class="text-right">Balance</th>
            </tr>
          </thead>
          <tbody>
            <t t-foreach="get_vendor_statement(doc.partner_id.id, start_date, end_date)" t-as="line">
              <tr>
                <td><t t-esc="line['date']"/></td>
                <td><t t-esc="line['invoice_number']"/></td>
                <td><t t-esc="line['description']"/></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(line['amount'])"/></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(line['paid_amount'])"/></td>
                <td class="text-right"><t t-esc="'{:.2f}'.format(line['balance'])"/></td>
              </tr>
            </t>
            <tr class="border-top border-dark font-weight-bold">
              <td colspan="5">Total</td>
              <td class="text-right"><t t-esc="'{:,.2f}'.format(sum_vendor_balance(doc.partner_id.id, start_date, end_date))"/></td>
            </tr>
          </tbody>
        </table>
      </div>
    </t>
  </t>
</template>
```

## Common Patterns

| Report | Data Method | Template Call |
|--------|------------|---|
| Invoice | `doc.invoice_line_ids` | `t-foreach="doc.invoice_line_ids"` |
| AR Aging | `get_aged_receivable()` | SQL query + loop |
| P&L | `get_pl_section()` | SQL query grouped by account |
| Balance Sheet | `get_balance_sheet_accounts()` | SQL query by type |
| Tax Report | `get_tax_summary()` | SQL join with tax table |
| Payment Stmt | `get_vendor_statement()` | SQL query filtered |

## Quick Reference

**Report Action (XML)**
```xml
<record id="action_report_NAME" model="ir.actions.report">
  <field name="name">Report Name</field>
  <field name="model">account.move</field>
  <field name="report_type">qweb-pdf</field>
  <field name="report_name">module.report_NAME</field>
</record>
```

**Python Data Methods**
```python
def get_aged_receivable(self):
    self.env.cr.execute("""SELECT ... FROM account_move WHERE ...""")
    return self.env.cr.dictfetchall()

def get_pl_section(self, section, start_date, end_date):
    # Fetch P&L data by section (revenue, cogs, expenses)
    pass

def get_balance_sheet_accounts(self, acc_type, as_of_date):
    # Fetch GL accounts by type (asset, liability, equity)
    pass
```

**QWeb Formatting**
```xml
<!-- Currency -->
<t t-esc="'{:,.2f}'.format(amount)"/>

<!-- Date -->
<t t-field="doc.date" t-options="{'widget': 'date', 'format': 'dd/MM/yyyy'}"/>

<!-- Conditional -->
<t t-if="line['age_days'] > 90" t-esc="'Overdue'"/>

<!-- Loop with totals -->
<t t-foreach="data" t-as="row">
  <tr><td t-esc="row['amount']"/></tr>
</t>
```

## Deployment Checklist

```
✓ Report actions registered (ir.actions.report)
✓ QWeb templates created
✓ Python data methods written
✓ SQL queries optimized
✓ Date filtering working
✓ Currency formatting correct
✓ PDF generation tested
✓ Multi-company filtering applied
✓ Pakistan tax rules applied (GST, WHT)
✓ Backup before deploying
```

## Version Notes (v16-19)

| Feature | v16 | v17+ | v18+ | v19+ |
|---------|-----|------|------|------|
| QWeb Reports | Full | Full | Full | Full |
| Invoice Template | Basic | Enhanced | Enhanced | Enhanced |
| SQL Queries | Manual | Manual | Optimized | Optimized |
| PDF Generation | wkhtmltopdf | wkhtmltopdf | Chrome | Chrome |
| Report Filters | Basic | Enhanced | Advanced | Advanced |
