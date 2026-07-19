# Odoo HR & Payroll Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE PATTERNS & WORKFLOWS

### **1. EMPLOYEE CONTRACTS & SALARY STRUCTURE**
```python
class HrContract(models.Model):
    _name = 'hr.contract'
    
    name = fields.Char(required=True)  # 'Ahmed - 2024'
    
    employee_id = fields.Many2one('hr.employee', required=True)
    company_id = fields.Many2one('res.company')
    
    # Contract dates
    date_start = fields.Date(required=True)
    date_end = fields.Date()  # NULL = ongoing
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('open', 'Active'),
        ('pending_resignation', 'Pending Resignation'),
        ('close', 'Closed'),
        ('cancel', 'Cancelled')
    ])
    
    # Salary structure
    struct_id = fields.Many2one('hr.payroll.structure', required=True)
    
    # Payment details
    wage = fields.Float(required=True)  # Base salary
    currency_id = fields.Many2one('res.currency')
    
    # Employment type
    type_id = fields.Many2one('hr.contract.type')
    # Types: Full-time, Part-time, Contract, Intern
    
    # Department
    department_id = fields.Many2one('hr.department')
    job_id = fields.Many2one('hr.job')
    
    # Working schedule
    resource_calendar_id = fields.Many2one('resource.calendar')  # Mon-Fri 9-5?
    
    def action_activate(self):
        """Activate contract"""
        if fields.Date.today() >= self.date_start:
            self.state = 'open'
        else:
            raise ValidationError("Contract starts in future")

class HrPayrollStructure(models.Model):
    _name = 'hr.payroll.structure'
    
    name = fields.Char(required=True)  # 'Salary Structure 2024'
    
    # Components
    rule_ids = fields.Many2many('hr.salary.rule')
    
    # Parent (inheritance)
    parent_id = fields.Many2one('hr.payroll.structure')

# SALARY COMPONENTS
class HrSalaryRule(models.Model):
    _name = 'hr.salary.rule'
    
    name = fields.Char(required=True)  # 'Basic Salary'
    code = fields.Char(required=True, unique=True)  # 'BASIC'
    
    # Rule category
    category_id = fields.Many2one('hr.salary.rule.category')
    
    # Computation
    amount_select = fields.Selection([
        ('fix', 'Fixed Amount'),
        ('percentage', 'Percentage'),
        ('code', 'Python Code'),
    ])
    
    amount_fix = fields.Float()  # Fixed amount
    amount_percentage = fields.Float()  # % of something
    amount_percentage_base = fields.Char()  # % of 'BASIC' (reference)
    
    # Python formula
    amount_python_compute = fields.Text()  # eval()'d code
    
    # Applicable
    active = fields.Boolean(default=True)
    sequence = fields.Integer()  # Order of calculation
    
    # Conditions
    condition_select = fields.Selection([
        ('none', 'Always'),
        ('range', 'Range'),
        ('python', 'Python'),
    ])
    
    condition_range_min = fields.Float()
    condition_range_max = fields.Float()
    condition_python = fields.Text()  # If eval() == True, apply rule

# Example: Monthly Salary Components
# BASIC: 50,000 (fixed)
# HRA (House Rent): 15,000 (fixed)
# CONVEYANCE: 5,000 (fixed)
# GROSS_SALARY: BASIC + HRA + CONVEYANCE = 70,000
#
# INCOME_TAX: 5% of GROSS = 3,500 (deduction)
# EOBI: 5% of GROSS = 3,500 (statutory, Pakistan)
# NET_SALARY: GROSS - TAX - EOBI = 63,000
```

### **2. PAYROLL RUNS & SALARY SLIPS**
```python
class HrPayslipRun(models.Model):
    _name = 'hr.payslip.run'
    
    name = fields.Char(auto=True)  # 'Salary Run - Sep 2024'
    
    date_start = fields.Date(required=True)  # Month start
    date_end = fields.Date(required=True)  # Month end
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('close', 'Close'),
        ('paid', 'Paid'),
        ('cancel', 'Cancelled')
    ])
    
    # Payroll batch
    slip_ids = fields.One2many('hr.payslip', 'payslip_run_id')
    
    # Amount summary
    total_amount = fields.Float(compute='_compute_totals')
    
    def action_draft_payslips(self):
        """
        Create draft payslips for all active employees
        """
        # Get all active contracts
        contracts = self.env['hr.contract'].search([
            ('date_start', '<=', self.date_end),
            ('date_end', '>=', self.date_start),
            ('state', '=', 'open'),
        ])
        
        for contract in contracts:
            self.env['hr.payslip'].create({
                'payslip_run_id': self.id,
                'employee_id': contract.employee_id.id,
                'contract_id': contract.id,
                'date_from': self.date_start,
                'date_to': self.date_end,
            })
    
    def action_confirm_sheet(self):
        """Lock payroll run - no more changes"""
        for slip in self.slip_ids:
            slip.action_payslip_done()
        self.state = 'close'

class HrPayslip(models.Model):
    _name = 'hr.payslip'
    
    name = fields.Char(auto=True)  # 'Salary Slip - Sep 2024'
    
    payslip_run_id = fields.Many2one('hr.payslip.run')
    employee_id = fields.Many2one('hr.employee', required=True)
    contract_id = fields.Many2one('hr.contract', required=True)
    
    # Period
    date_from = fields.Date(required=True)
    date_to = fields.Date(required=True)
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('verify', 'Waiting for Verification'),
        ('done', 'Done'),
        ('cancel', 'Rejected')
    ])
    
    # Salary components (lines)
    line_ids = fields.One2many('hr.payslip.line', 'slip_id')
    
    # Totals
    amount_basic = fields.Float(compute='_compute_totals')
    amount_gross = fields.Float(compute='_compute_totals')  # Earnings
    amount_deduct = fields.Float(compute='_compute_totals')  # Deductions
    amount_net = fields.Float(compute='_compute_totals')  # Net pay
    
    # Days
    worked_days_ids = fields.One2many('hr.payslip.worked_days', 'payslip_id')
    
    def compute_sheet(self):
        """
        Calculate payslip:
        1. Get salary structure from contract
        2. Apply each rule (in order)
        3. Create line items
        4. Sum totals
        """
        self.line_ids.unlink()  # Clear old lines
        
        structure = self.contract_id.struct_id
        
        # Track values for % calculations
        payslip_values = {}
        
        for rule in structure.rule_ids.sorted('sequence'):
            # Check conditions
            if not self._eval_condition(rule):
                continue
            
            # Calculate amount
            if rule.amount_select == 'fix':
                amount = rule.amount_fix
            elif rule.amount_select == 'percentage':
                # % of another component
                base_rule_code = rule.amount_percentage_base
                base_amount = payslip_values.get(base_rule_code, 0)
                amount = base_amount * (rule.amount_percentage / 100)
            elif rule.amount_select == 'code':
                # Python eval
                amount = eval(rule.amount_python_compute, {
                    'payslip': self,
                    'employee': self.employee_id,
                    'contract': self.contract_id,
                    'rules': payslip_values,
                })
            
            # Store for reference
            payslip_values[rule.code] = amount
            
            # Create line
            self.env['hr.payslip.line'].create({
                'slip_id': self.id,
                'salary_rule_id': rule.id,
                'code': rule.code,
                'name': rule.name,
                'amount': amount,
                'category_id': rule.category_id.id,
            })

class HrPayslipLine(models.Model):
    _name = 'hr.payslip.line'
    
    slip_id = fields.Many2one('hr.payslip')
    salary_rule_id = fields.Many2one('hr.salary.rule')
    
    name = fields.Char()  # 'Basic Salary'
    code = fields.Char()  # 'BASIC'
    
    amount = fields.Float()
    
    category_id = fields.Many2one('hr.salary.rule.category')
    # Categories: 'Gross', 'Deductions', 'Net'

class HrPayslipWorkedDays(models.Model):
    _name = 'hr.payslip.worked_days'
    
    payslip_id = fields.Many2one('hr.payslip')
    
    # Day type
    work_entry_type_id = fields.Many2one('hr.work.entry.type')
    # Types: Normal, Sick Leave, Annual Leave, Holiday
    
    # Attendance
    number_of_days = fields.Float()  # How many days worked/leave
    number_of_hours = fields.Float()  # Total hours
    
    # Impact on salary
    is_paid = fields.Boolean()  # Paid day or unpaid leave?
    deduction = fields.Float()  # Amount deducted if unpaid
```

### **3. ATTENDANCE & TIMESHEETS**
```python
class HrAttendance(models.Model):
    _name = 'hr.attendance'
    
    employee_id = fields.Many2one('hr.employee', required=True)
    
    # Time tracking
    check_in = fields.Datetime(required=True)
    check_out = fields.Datetime()
    
    worked_hours = fields.Float(compute='_compute_hours')
    
    def _compute_hours(self):
        """Hours worked = check_out - check_in"""
        for att in self:
            if att.check_in and att.check_out:
                delta = att.check_out - att.check_in
                att.worked_hours = delta.total_seconds() / 3600
            else:
                att.worked_hours = 0

# TIMESHEETS (for project billing)
class AccountAnalyticLine(models.Model):
    _name = 'account.analytic.line'
    
    employee_id = fields.Many2one('hr.employee')
    project_id = fields.Many2one('project.project')
    task_id = fields.Many2one('project.task')
    
    # Time
    date = fields.Date()
    unit_amount = fields.Float()  # Hours spent
    
    # Description
    name = fields.Text()  # 'Feature implementation'
    
    # For billing
    amount = fields.Float(compute='_compute_amount')  # hours * hourly_rate
    
    # General accounts (cost center)
    account_id = fields.Many2one('account.analytic.account')

# Timesheet entry for daily tracking:
# Project: Website Redesign
# Task: Homepage mockup
# Date: 2024-09-15
# Hours: 8
# Rate: $50/hour
# Amount: $400 (billable to customer)
```

### **4. LEAVE MANAGEMENT**
```python
class HrLeaveType(models.Model):
    _name = 'hr.leave.type'
    
    name = fields.Char(required=True)  # 'Annual Leave', 'Sick Leave'
    
    # Allocation
    allocation_type = fields.Selection([
        ('regular', 'Regular'),
        ('accrual', 'Accrual-based'),
    ])
    
    # Annual entitlement
    yearly_allocation = fields.Float()  # Days per year
    
    # Carryover
    max_carryover_days = fields.Float()  # Max days to next year
    carryover_percentage = fields.Float()  # 50% carryover
    
    # Rules
    max_leaves_per_request = fields.Float()
    allow_negative_balance = fields.Boolean()
    
    # Deduction
    deduct_from_payroll = fields.Boolean()  # Reduce salary if unpaid

class HrLeave(models.Model):
    _name = 'hr.leave'
    
    employee_id = fields.Many2one('hr.employee', required=True)
    leave_type_id = fields.Many2one('hr.leave.type', required=True)
    
    # Dates
    date_from = fields.Datetime(required=True)
    date_to = fields.Datetime(required=True)
    
    number_of_days = fields.Float(compute='_compute_days')
    
    state = fields.Selection([
        ('draft', 'To Submit'),
        ('confirm', 'Waiting Approval'),
        ('refuse', 'Refused'),
        ('validate1', 'Manager Approved'),
        ('validate', 'HR Approved'),
        ('cancel', 'Cancelled')
    ])
    
    reason = fields.Char()
    
    # Approval
    manager_id = fields.Many2one('hr.employee')
    
    def action_request(self):
        """Submit leave request"""
        self.state = 'confirm'
        # Notify manager
    
    def approve_leave(self):
        """Manager approves"""
        self.state = 'validate1'
    
    def action_validate(self):
        """HR validates"""
        # Check balance
        available = self.employee_id.get_leave_balance(self.leave_type_id)
        
        if available < self.number_of_days:
            raise ValidationError(
                f"Only {available} days available, "
                f"requested {self.number_of_days}"
            )
        
        self.state = 'validate'
        
        # Deduct from balance
        self.env['hr.leave.allocation'].create({
            'employee_id': self.employee_id.id,
            'leave_type_id': self.leave_type_id.id,
            'number_of_days': -self.number_of_days,  # Negative = deduction
            'leave_id': self.id,
        })

class HrLeaveAllocation(models.Model):
    _name = 'hr.leave.allocation'
    
    employee_id = fields.Many2one('hr.employee')
    leave_type_id = fields.Many2one('hr.leave.type')
    
    # Add or subtract
    number_of_days = fields.Float()  # +20 = allocate, -5 = use leave
    
    date_from = fields.Date()
    
    # Type
    allocation_type = fields.Selection([
        ('regular', 'Regular'),
        ('accrual', 'Accrual'),
    ])

# Leave balance tracking:
# Annual Leave:
#   + 20 days allocated (Jan 1)
#   - 2 days taken (Sep 15)
#   - 3 days taken (Oct 20)
#   = 15 days available
```

### **5. STATUTORY DEDUCTIONS (PAKISTAN EXAMPLE)**
```python
"""
Pakistan statutory deductions:
- EOBI (Employees' Old Age Benefit) - 5%
- SESSI (Employees' Social Security) - varies by province
- Income Tax - progressive (5% - 35%)
- GST (General Sales Tax) - on invoices
- Withholding Tax - on payments
"""

class HrSalaryRule(models.Model):
    
    # EOBI Rule
    # Code: 'EOBI'
    # Amount: 5% of GROSS
    # Category: Deductions
    
    # Income Tax (Withholding Tax - WHT)
    # Code: 'TAX'
    # Computation: Progressive tax calculation
    # 0-600,000: 0%
    # 600k-1.2m: 5%
    # 1.2m-2.4m: 10%
    # 2.4m+: 17.5%

def calculate_income_tax(gross_salary):
    """Progressive tax brackets (Pakistan)"""
    tax = 0
    
    if gross_salary > 2_400_000:
        tax += (gross_salary - 2_400_000) * 0.175
        gross_salary = 2_400_000
    
    if gross_salary > 1_200_000:
        tax += (gross_salary - 1_200_000) * 0.10
        gross_salary = 1_200_000
    
    if gross_salary > 600_000:
        tax += (gross_salary - 600_000) * 0.05
    
    return tax

# Tax relief (government allowance)
class HrTaxDeclaration(models.Model):
    _name = 'hr.tax.declaration'
    
    employee_id = fields.Many2one('hr.employee')
    
    # Deductions
    life_insurance = fields.Float()  # LIC premium
    mortgage_interest = fields.Float()
    medical_expenses = fields.Float()
    
    # Used for tax calculation
    total_deduction = fields.Float(compute='_compute_deduction')
    
    def _compute_deduction(self):
        self.total_deduction = (
            self.life_insurance + 
            self.mortgage_interest + 
            self.medical_expenses
        )
    
    # Applicable deduction (limited)
    deductible_amount = fields.Float(
        compute='_compute_deductible'
    )  # Max 25% of income

class HrPayslip(models.Model):
    
    def compute_tax(self):
        """Apply tax with relief"""
        gross = self.amount_gross
        
        # Get tax declaration
        tax_decl = self.employee_id.tax_declaration_ids.filtered(
            lambda d: d.date >= self.date_from
        )[0] if self.employee_id.tax_declaration_ids else None
        
        relief = tax_decl.deductible_amount if tax_decl else 0
        
        # Taxable income
        taxable = gross - relief
        
        # Calculate tax
        tax = calculate_income_tax(taxable)
        
        return tax
```

### **6. SALARY ADVANCE & LOANS**
```python
class HrSalaryAdvance(models.Model):
    _name = 'hr.salary.advance'
    
    employee_id = fields.Many2one('hr.employee', required=True)
    
    # Advance details
    advance_amount = fields.Float(required=True)
    advance_date = fields.Date(default=fields.Date.today)
    
    reason = fields.Text()
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirm', 'Waiting Approval'),
        ('approve', 'Approved'),
        ('refuse', 'Refused'),
        ('paid', 'Paid'),
    ])
    
    # Repayment
    number_of_installments = fields.Integer()  # Over how many months?
    installment_amount = fields.Float(compute='_compute_installment')
    
    def action_approve(self):
        """Approve and disburse"""
        self.state = 'approve'
        
        # Create payment
        self.env['hr.salary.advance.payment'].create({
            'advance_id': self.id,
            'amount': self.advance_amount,
            'date': fields.Date.today(),
        })
        
        # Create deduction rules for repayment
        for month in range(self.number_of_installments):
            self.env['hr.salary.advance.deduction'].create({
                'advance_id': self.id,
                'payslip_month': month + 1,
                'deduction_amount': self.installment_amount,
            })
```

### **7. PAYROLL JOURNAL ENTRIES**
```python
class HrPayslip(models.Model):
    
    move_id = fields.Many2one('account.move')  # GL entry
    
    def action_payslip_done(self):
        """
        When payslip done:
        1. Create GL entry
        2. Debit salary expense
        3. Credit cash/bank
        """
        # Salary expense account
        expense_account = self.contract_id.struct_id.account_id
        
        # Create GL entry
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'line_ids': [
                # Salary expense (debit)
                (0, 0, {
                    'account_id': expense_account.id,
                    'debit': self.amount_gross,
                    'name': f"Salary {self.employee_id.name}",
                }),
                # Bank (credit)
                (0, 0, {
                    'account_id': SALARY_PAYABLE_ACCOUNT.id,
                    'credit': self.amount_net,
                }),
                # Tax payable (credit)
                (0, 0, {
                    'account_id': TAX_PAYABLE_ACCOUNT.id,
                    'credit': self.get_tax_amount(),
                }),
                # EOBI payable (credit)
                (0, 0, {
                    'account_id': EOBI_PAYABLE_ACCOUNT.id,
                    'credit': self.get_eobi_amount(),
                }),
            ]
        })
        
        move.action_post()
        self.move_id = move.id
```

### **8. PAYMENT RUNS**
```python
class HrPaymentRun(models.Model):
    _name = 'hr.payment.run'
    
    name = fields.Char(auto=True)  # 'Payment Run - Sep 2024'
    
    # Linked payslip run
    payslip_run_id = fields.Many2one('hr.payslip.run')
    
    # All payslips to pay
    payslip_ids = fields.Many2many('hr.payslip')
    
    # Total amount
    total_amount = fields.Float(compute='_compute_total')
    
    # Payments
    payment_ids = fields.One2many('account.payment', 'payment_run_id')
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirm', 'Confirmed'),
        ('done', 'Done'),
    ])
    
    def action_generate_payments(self):
        """
        Create payment for each employee
        """
        for payslip in self.payslip_ids:
            payment = self.env['account.payment'].create({
                'payment_type': 'outbound',  # Pay out salary
                'partner_id': payslip.employee_id.user_id.partner_id.id,
                'amount': payslip.amount_net,
                'journal_id': SALARY_BANK_JOURNAL.id,
                'payment_run_id': self.id,
            })
            payment.action_post()  # Post payment
```

---

## 📚 **KEY MODELS CHEAT SHEET**

| Model | Purpose |
|-------|---------|
| **hr.employee** | Employee record |
| **hr.contract** | Employment contract |
| **hr.salary.rule** | Salary component |
| **hr.payslip** | Monthly payslip |
| **hr.leave** | Leave request |
| **hr.attendance** | Check-in/out |
| **hr.payslip.run** | Payroll batch |

---

## 🔧 **COMMON TASKS**

### **Create Payroll Run**
```python
run = self.env['hr.payslip.run'].create({
    'date_start': '2024-09-01',
    'date_end': '2024-09-30',
})
run.action_draft_payslips()  # Create payslips for all employees
```

### **Calculate Payslip**
```python
payslip = self.env['hr.payslip'].browse(payslip_id)
payslip.compute_sheet()  # Calculate all components
```

### **Approve Leave**
```python
leave = self.env['hr.leave'].browse(leave_id)
leave.action_request()  # Submit
leave.approve_leave()  # Manager approves
leave.action_validate()  # HR validates
```

---

## ⚠️ **CRITICAL RULES**

1. **Active contract**: Required to create payslip
2. **Salary structure**: Defines all components
3. **Leave balance**: Must be sufficient before approval
4. **Tax calculation**: Progressive brackets per country
5. **Statutory deductions**: Mandatory (EOBI, tax, etc.)

---

**Skill Status:** ✅ Ready for all HR & Payroll tasks
