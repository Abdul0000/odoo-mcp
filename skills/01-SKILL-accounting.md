# Odoo Accounting & Finance Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE PATTERNS & WORKFLOWS

### **1. INVOICE LIFECYCLE**
```python
# Full cycle: Draft → Posted → Paid/Cancelled
# Trigger: Sales/Purchase orders → Auto-create account.move

class AccountMove(models.Model):
    _name = 'account.move'
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancel', 'Cancelled')
    ])
    move_type = fields.Selection([
        ('entry', 'Journal Entry'),
        ('out_invoice', 'Customer Invoice'),
        ('in_invoice', 'Vendor Bill'),
        ('out_refund', 'Credit Note'),
        ('in_refund', 'Vendor Refund')
    ])
    
    # Auto-balance: sum(debit) = sum(credit)
    line_ids = fields.One2many('account.move.line', 'move_id')
    
    def action_post(self):
        """Lock invoice, prevent edits, trigger accounting"""
        # Validate debit = credit
        # Create GL entries
        # Update balances
        self.state = 'posted'

# Key constraint: Balanced journal entry (±0.01 rounding tolerance)
```

### **2. JOURNAL ENTRY CREATION & GL POSTING**
```python
# All transactions create journal entries:
# Invoice → account.move + account.move.line
# Each line: Debit OR Credit, GL account, amount

class AccountMoveLine(models.Model):
    _name = 'account.move.line'
    
    account_id = fields.Many2one('account.account', required=True)
    debit = fields.Float(default=0)
    credit = fields.Float(default=0)
    
    @api.constrains('debit', 'credit')
    def _check_balance(self):
        # Rule: debit XOR credit (not both, not neither)
        if self.debit and self.credit:
            raise ValidationError("Cannot have both debit & credit")

# Pattern: Bi-directional GL posting
# Asset increase → Debit
# Liability increase → Credit
# Revenue → Credit
# Expense → Debit
```

### **3. ACCOUNTING EQUATION (A = L + E)**
```python
# Chart of Accounts hierarchy by type:
# 1000-1999: Assets (Debit balances)
# 2000-2999: Liabilities (Credit balances)
# 3000-3999: Equity
# 4000-4999: Revenue (Credit balances)
# 5000-5999: Expenses (Debit balances)

class AccountAccount(models.Model):
    _name = 'account.account'
    
    code = fields.Char(required=True)  # e.g., '1100'
    name = fields.Char(required=True)
    account_type = fields.Selection([
        ('asset_current', 'Current Asset'),
        ('asset_non_current', 'Non-Current Asset'),
        ('liability_current', 'Current Liability'),
        ('liability_non_current', 'Non-Current Liability'),
        ('equity', 'Equity'),
        ('income', 'Income'),
        ('expense', 'Expense'),
        ('off_balance', 'Off-Balance')
    ])
    
    def get_balance(self):
        """Sum of debit-credit for all posted moves"""
        # Queries account.move.line where account_id = self
        return sum(lines.debit - lines.credit)
```

### **4. MULTI-CURRENCY & EXCHANGE RATES**
```python
class AccountMove(models.Model):
    
    currency_id = fields.Many2one('res.currency')  # Document currency
    company_currency_id = fields.Many2one(
        'res.currency', 
        related='company_id.currency_id'
    )
    
    # Store conversion rate at time of invoice
    date = fields.Date()
    
    @api.depends('currency_id', 'date')
    def _compute_exchange_rate(self):
        """Fetch rate for date"""
        rate = self.currency_id._get_conversion_rate(
            from_currency=self.currency_id,
            to_currency=self.company_currency_id,
            company=self.company_id,
            date=self.date
        )

# Tax impact: Multi-currency → Currency gain/loss
# Invoice EUR 100 @ rate 1.1 = Company $ 110
# Paid EUR 100 @ rate 1.15 = Company $ 115
# Currency loss: $5 → Realized gain/loss account
```

### **5. TAX CALCULATION & COMPLIANCE**
```python
class AccountTax(models.Model):
    _name = 'account.tax'
    
    name = fields.Char(required=True)  # e.g., 'VAT 17%'
    type_tax_use = fields.Selection([
        ('sale', 'Sales Tax'),
        ('purchase', 'Purchase Tax'),
        ('none', 'None')
    ])
    amount = fields.Float()  # 17.0 for 17%
    amount_type = fields.Selection([
        ('fixed', 'Fixed'),
        ('percent', 'Percentage'),
        ('group', 'Group of Taxes')
    ])

# Invoice line applies tax
# Auto-calculation: Amount * tax% = tax_amount
# Total = amount + tax

# Pakistan GST: 17% standard rate
# Withholding Tax: 1-15% on invoices
# Sales Tax: Regional variations

class AccountMoveLine(models.Model):
    
    price_unit = fields.Float()
    quantity = fields.Float()
    tax_ids = fields.Many2many('account.tax')
    
    @api.depends('price_unit', 'quantity', 'tax_ids')
    def _compute_totals(self):
        """Calculate before & after tax"""
        price_subtotal = self.price_unit * self.quantity
        
        tax_amount = 0
        for tax in self.tax_ids:
            if tax.amount_type == 'percent':
                tax_amount += price_subtotal * (tax.amount / 100)
        
        self.price_total = price_subtotal + tax_amount
```

### **6. BANK RECONCILIATION**
```python
class AccountBankStatementLine(models.Model):
    _name = 'account.bank.statement.line'
    
    amount = fields.Float()  # Debit = +, Credit = -
    payment_ref = fields.Char()
    date = fields.Date()
    
    # Link to invoice/payment
    reconcile_model_id = fields.Many2one('account.reconcile.model')

class AccountReconcile(models.Model):
    """Match bank stmt lines to payments"""
    
    debit_ids = fields.Many2many('account.move.line')  # Payments
    credit_ids = fields.Many2many('account.move.line')  # Credits
    
    # 2-way match: Bank stmt + Payment
    # Reconciled = matched, clears from AR/AP
```

### **7. PAYMENT WORKFLOWS**
```python
class AccountPayment(models.Model):
    _name = 'account.payment'
    
    move_type = fields.Selection([
        ('outbound', 'Send Money'),
        ('inbound', 'Receive Money')
    ])
    payment_type = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted')
    ])
    
    partner_id = fields.Many2one('res.partner')
    amount = fields.Float()
    
    # Create GL entries when posted
    move_id = fields.Many2one('account.move')
    
    def action_post(self):
        """
        1. Create account.move
        2. Debit/Credit bank account
        3. Link to invoice
        4. Mark invoice as paid
        """
        self.move_id = self.env['account.move'].create({
            'move_type': 'entry',
            'line_ids': [
                (0, 0, {
                    'account_id': self.journal_id.default_debit_account_id.id,
                    'credit': self.amount,  # Bank paid out
                }),
                (0, 0, {
                    'account_id': self.invoice_ids.account_id.id,
                    'debit': self.amount,   # AR/AP cleared
                })
            ]
        })
        self.move_id.action_post()
```

### **8. INVOICE MATCHING (3-Way Match)**
```python
"""
3-Way Match: PO → Receipt (GRN) → Vendor Bill
- PO quantity & price vs
- GRN quantity vs
- Bill quantity & price
"""

class PurchaseOrder(models.Model):
    invoice_lines = fields.One2many('account.move.line', ...)
    
    def action_create_bill(self):
        """Auto-generate vendor bill from PO"""
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_id.id,
            'purchase_id': self.id,
            'line_ids': [
                (0, 0, {
                    'product_id': line.product_id.id,
                    'quantity': line.product_qty,
                    'price_unit': line.price_unit,
                })
                for line in self.order_line
            ]
        })
        # Validates: Bill qty <= PO qty
        # Flag if price variance > threshold
```

### **9. FINANCIAL REPORTS (P&L, BALANCE SHEET)**
```python
class AccountReport(models.TransientModel):
    """Dynamic report generation"""
    
    @api.model
    def get_profit_loss(self, date_from, date_to):
        """
        P&L = Revenue - Expenses
        
        Revenue (4000-4999 accounts):
            Sales: $100,000
            Services: $50,000
        Expenses (5000-5999):
            COGS: ($40,000)
            Salary: ($30,000)
        Net Income: $80,000
        """
        revenue_lines = self.env['account.move.line'].search([
            ('account_id.account_type', '=', 'income'),
            ('date', '>=', date_from),
            ('date', '<=', date_to)
        ])
        
        revenue = sum(revenue_lines.mapped('credit'))
        
        expense_lines = self.env['account.move.line'].search([
            ('account_id.account_type', '=', 'expense'),
            ('date', '>=', date_from),
            ('date', '<=', date_to)
        ])
        
        expenses = sum(expense_lines.mapped('debit'))
        
        return revenue - expenses

    @api.model
    def get_balance_sheet(self, date):
        """
        Balance Sheet = Assets | Liabilities + Equity
        
        Assets (1000-1999):
            Cash: $50,000
            AR: $30,000
        Liabilities (2000-2999):
            AP: ($20,000)
        Equity: ($60,000)
        Total: $0
        """
        assets = self.env['account.move.line'].search([
            ('account_id.account_type', 'in', ['asset_current', 'asset_non_current']),
            ('date', '<=', date)
        ])
        
        liabilities = self.env['account.move.line'].search([
            ('account_id.account_type', 'in', ['liability_current', 'liability_non_current']),
            ('date', '<=', date)
        ])
        
        return {
            'assets': sum(assets.mapped('debit') - assets.mapped('credit')),
            'liabilities': sum(liabilities.mapped('credit') - liabilities.mapped('debit')),
        }
```

### **10. COMPANY & MULTI-COMPANY ACCOUNTING**
```python
class ResCompany(models.Model):
    
    currency_id = fields.Many2one('res.currency')
    country_id = fields.Many2one('res.country')  # For tax jurisdiction
    
    # Chart of accounts per company
    chart_template_id = fields.Many2one('account.chart.template')

class AccountMove(models.Model):
    
    company_id = fields.Many2one('res.company', required=True)
    
    # Journal must belong to company
    journal_id = fields.Many2one('account.journal')
    
    # Constraint: All lines belong to company
    @api.constrains('company_id', 'line_ids')
    def _check_company(self):
        for line in self.line_ids:
            if line.account_id.company_id != self.company_id:
                raise ValidationError("Account must belong to invoice company")
```

---

## 📚 **KEY MODELS CHEAT SHEET**

| Model | Purpose | Key Fields |
|-------|---------|-----------|
| **account.move** | Invoices/Bills | move_type, state, line_ids |
| **account.move.line** | GL entries | account_id, debit, credit |
| **account.account** | GL accounts | code, account_type, balance |
| **account.tax** | Tax rates | amount, type_tax_use |
| **account.payment** | Payments | amount, journal_id |
| **account.journal** | Journals | type, default accounts |
| **account.reconcile** | Bank reconciliation | debit_ids, credit_ids |
| **account.bank.statement** | Bank feeds | line_ids |

---

## 🔧 **COMMON TASKS**

### **Create Customer Invoice Programmatically**
```python
move = self.env['account.move'].create({
    'move_type': 'out_invoice',
    'partner_id': partner.id,
    'invoice_date': fields.Date.today(),
    'line_ids': [
        (0, 0, {
            'product_id': product.id,
            'quantity': 10,
            'price_unit': 100,
            'tax_ids': [(6, 0, [tax.id])],
        })
    ]
})
move.action_post()  # Post to GL
```

### **Fetch Account Balance**
```python
account = self.env['account.account'].search([('code', '=', '1100')])
balance = account.get_balance()  # Returns current balance
```

### **Record Payment Against Invoice**
```python
payment = self.env['account.payment'].create({
    'payment_type': 'inbound',
    'partner_id': invoice.partner_id.id,
    'amount': invoice.amount_residual,
    'invoice_ids': [(6, 0, [invoice.id])],
})
payment.action_post()  # Creates journal entry
# Invoice marked as paid automatically
```

---

## ⚠️ **CRITICAL RULES**

1. **Debit = Credit**: Every journal entry must balance
2. **Posted = Locked**: Cannot edit posted moves (must cancel/reverse)
3. **GL Hierarchy**: 5-digit COA structure (1000-9999)
4. **Tax on Lines**: Applied per invoice line, not total
5. **Multi-Currency**: Exchange rates captured at invoice date
6. **Reconciliation**: Unreconciled items appear in AR/AP aging

---

## 🚀 **QUICK REFERENCE**

- **Default GL entries**: Configured in journal/payment method
- **Auto-reconciliation**: Bank feeds match to payments automatically
- **Fiscal period**: Lock accounting after month-end to prevent adjustments
- **Rounding**: Odoo allows ±0.01 tolerance on journal entries
- **Reverse journal entries**: Use "reverse move" to undo instead of deleting

---

**Skill Status:** ✅ Ready for all Accounting tasks
