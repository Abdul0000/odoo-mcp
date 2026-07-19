# Odoo Manufacturing (MRP) Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE PATTERNS & WORKFLOWS

### **1. BILL OF MATERIALS (BoM) HIERARCHY**
```python
# BoM = Recipe for product
# 1-level: Component → Product
# Multi-level: Raw → Sub-assembly → Final product

class MrpBom(models.Model):
    _name = 'mrp.bom'
    
    product_id = fields.Many2one('product.product', required=True)
    product_qty = fields.Float(default=1)  # Finished qty this BoM produces
    
    # Hierarchical components
    bom_line_ids = fields.One2many('mrp.bom.line', 'bom_id')
    
    type = fields.Selection([
        ('normal', 'Manufacture'),
        ('phantom', 'Phantom (invisible)'),
        ('subcontract', 'Subcontracting')
    ])
    
    # Scope: Which operations use this BoM?
    operation_ids = fields.Many2many('mrp.routing.workcenter')
    
    def _bom_explode(self, quantity):
        """Recursive expand BoM to get all raw materials"""
        # If sub-assembly has BoM, expand it too
        # Returns flat list of all components needed
        
        components = []
        for line in self.bom_line_ids:
            component_qty = (quantity / self.product_qty) * line.product_qty
            
            # Check if component has sub-BoM
            if line.product_id.bom_ids:
                components += line.product_id.bom_ids[0]._bom_explode(
                    component_qty
                )
            else:
                components.append({
                    'product_id': line.product_id,
                    'quantity': component_qty,
                })
        
        return components

class MrpBomLine(models.Model):
    _name = 'mrp.bom.line'
    
    bom_id = fields.Many2one('mrp.bom', required=True)
    product_id = fields.Many2one('product.product', required=True)
    product_qty = fields.Float()  # Qty of component per 1 unit of finished product

# Example: Produce 1 Chair
# ├─ 4x Leg (product.product)
# ├─ 1x Seat (sub-assembly)
# │  └─ 1x Wood (raw material)
# │  └─ 1x Fabric (raw material)
# └─ 8x Screw (raw material)
```

### **2. PRODUCTION ORDERS (mrp.production)**
```python
class MrpProduction(models.Model):
    _name = 'mrp.production'
    
    name = fields.Char(auto=True)  # MO/2024/00001
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
        ('progress', 'In Progress'),
        ('done', 'Done'),
        ('cancel', 'Cancelled')
    ])
    
    product_id = fields.Many2one('product.product', required=True)
    product_qty = fields.Float(required=True)
    
    # Linked BoM (auto-selected if exists)
    bom_id = fields.Many2one('mrp.bom')
    
    # Source document (Sales Order)
    origin = fields.Char()  # SO reference
    
    # Actual components needed
    move_raw_ids = fields.One2many(
        'stock.move', 'raw_material_production_id'
    )  # Components to consume
    
    # Produced qty tracking
    move_finished_ids = fields.One2many(
        'stock.move', 'production_id'
    )  # Finished product to produce

# WORKFLOW
def action_confirm(self):
    """1. Confirm MO → Auto-create stock moves"""
    # Explode BoM → Get components
    components = self.bom_id._bom_explode(self.product_qty)
    
    # Create stock.move for each component (need to withdraw)
    for component in components:
        self.env['stock.move'].create({
            'name': f"Consume {component['product_id'].name}",
            'product_id': component['product_id'].id,
            'product_uom_qty': component['quantity'],
            'location_id': STOCK_LOCATION.id,  # From stock
            'location_dest_id': PRODUCTION_LOCATION.id,  # To production
            'raw_material_production_id': self.id,
        })
    
    # Create stock.move for finished product (to produce)
    self.env['stock.move'].create({
        'name': f"Produce {self.product_id.name}",
        'product_id': self.product_id.id,
        'product_uom_qty': self.product_qty,
        'location_id': PRODUCTION_LOCATION.id,
        'location_dest_id': FINISHED_LOCATION.id,
        'production_id': self.id,
    })

def action_start(self):
    """2. Start production → Create work orders if routing exists"""
    if self.bom_id.operation_ids:
        for idx, operation in enumerate(self.bom_id.operation_ids):
            self.env['mrp.workorder'].create({
                'production_id': self.id,
                'workcenter_id': operation.workcenter_id.id,
                'operation_id': operation.id,
                'sequence': idx,
            })

def button_mark_done(self):
    """3. Complete production → Finalize moves, create GL entries"""
    # Move components from production location → scrap (if excess)
    # Move finished product to finished location
    # Update inventory
    # Create accounting entries if costing enabled
    self.state = 'done'
```

### **3. WORK ORDERS & ROUTING**
```python
class MrpRouting(models.Model):
    _name = 'mrp.routing'
    
    name = fields.Char()
    operation_ids = fields.One2many('mrp.routing.workcenter', 'routing_id')

class MrpRoutingWorkcenter(models.Model):
    _name = 'mrp.routing.workcenter'
    
    routing_id = fields.Many2one('mrp.routing')
    workcenter_id = fields.Many2one('mrp.workcenter', required=True)
    sequence = fields.Integer()  # Order of operations
    
    time_mode = fields.Selection([
        ('manual', 'Manual'),
        ('auto', 'Automatic'),
    ])
    time_cycle = fields.Float()  # Minutes per unit
    
    # E.g., Assemble Chair:
    # Op 1: Cut wood (Saw station) - 5 min
    # Op 2: Sand (Sander) - 3 min
    # Op 3: Assemble (Assembly line) - 10 min
    # Op 4: QA (Quality) - 2 min

class MrpWorkcenter(models.Model):
    _name = 'mrp.workcenter'
    
    name = fields.Char(required=True)
    time_zone = fields.Selection(TZ_LIST)
    
    # Capacity planning
    capacity = fields.Float()  # e.g., 10 (units per shift)
    oee = fields.Float()  # Overall Equipment Effectiveness %
    
    # Costs
    costs_hour = fields.Float()  # Hourly labor cost
    costs_hour_ids = fields.One2many('mrp.workcenter.cost', 'workcenter_id')

class MrpWorkorder(models.Model):
    _name = 'mrp.workorder'
    
    production_id = fields.Many2one('mrp.production')
    workcenter_id = fields.Many2one('mrp.workcenter')
    operation_id = fields.Many2one('mrp.routing.workcenter')
    
    state = fields.Selection([
        ('ready', 'Ready'),
        ('pending', 'Pending'),
        ('progress', 'In Progress'),
        ('done', 'Done'),
        ('cancel', 'Cancelled')
    ])
    
    # Time tracking
    date_start = fields.Datetime()
    date_end = fields.Datetime()
    duration_expected = fields.Float()  # From routing
    duration = fields.Float(compute='_compute_duration')  # Actual
    
    employee_ids = fields.Many2many('hr.employee')  # Assigned workers
    
    # Quality checks
    quality_check_ids = fields.One2many('quality.check', 'workorder_id')
    
    def action_start(self):
        self.date_start = fields.Datetime.now()
        self.state = 'progress'
    
    def action_finish(self):
        self.date_end = fields.Datetime.now()
        self.state = 'done'

# Capacity check: Total time of all operations ≤ Workcenter capacity
```

### **4. PRODUCTION WORKFLOWS (1-STEP, 2-STEP, 3-STEP)**
```python
"""
1-STEP: Raw → Finished (immediate)
2-STEP: Raw → Production location → Finished
3-STEP: Raw → Production start → Production end → Finished
"""

class StockWarehouseConfig(models.Model):
    """Warehouse operations config"""
    
    # Manufacturing workflow
    manufacture_steps = fields.Selection([
        ('mrp_one_step', '1-Step: Raw → Finished'),
        ('mrp_two_step', '2-Step: Raw → Production → Finished'),
        ('pbm_two_step', '2-Step with work orders'),
    ])
    
    # 1-STEP: Simple, no intermediate
    # - MO created
    # - Moves created from stock → finished
    # - No "production in progress" location
    
    # 2-STEP: Most common
    # - Components consumed to "Production" location
    # - Finished product moved from "Production" to storage
    # - Allows quality checks mid-production
    
    # 3-STEP (with work orders):
    # - More granular control
    # - Each workcenter is intermediate location
    # - Useful for batch productions

# Default: 2-STEP (balance of visibility + simplicity)
```

### **5. INVENTORY VALUATION & PRODUCTION COSTING**
```python
class ProductProduct(models.Model):
    
    cost_method = fields.Selection([
        ('standard', 'Standard Cost'),
        ('fifo', 'FIFO'),
        ('lifo', 'LIFO'),
        ('average', 'Average Cost')
    ])
    
    standard_price = fields.Float()  # Fixed cost for planning
    
    # Qty on hand
    qty_available = fields.Float(compute='_compute_quantities')

class MrpProduction(models.Model):
    
    @api.depends('move_raw_ids')
    def _compute_costs(self):
        """Calculate production cost"""
        raw_cost = sum(
            move.product_id.standard_price * move.product_uom_qty
            for move in self.move_raw_ids
        )
        
        # Add labor cost from work orders
        labor_cost = sum(
            (wo.duration / 60) * wo.workcenter_id.costs_hour
            for wo in self.workorder_ids
        )
        
        total_cost = raw_cost + labor_cost
        unit_cost = total_cost / self.product_qty
        
        return {
            'raw_cost': raw_cost,
            'labor_cost': labor_cost,
            'total_cost': total_cost,
            'unit_cost': unit_cost,
        }
    
    def action_post_inventory(self):
        """Create GL entries for inventory adjustment"""
        # Debit: Finished goods (asset)
        # Credit: COGS / Raw materials (expense)
        
        # Accounts auto-configured in product category
        account_debit = self.product_id.categ_id.property_stock_account_input_categ_id
        account_credit = self.product_id.categ_id.property_stock_account_valuation_id
        
        self.env['account.move'].create({
            'move_type': 'entry',
            'line_ids': [
                (0, 0, {
                    'account_id': account_debit.id,
                    'debit': self.total_cost,
                    'name': f"Production cost {self.name}",
                }),
                (0, 0, {
                    'account_id': account_credit.id,
                    'credit': self.total_cost,
                })
            ]
        }).action_post()
```

### **6. QUALITY CONTROL**
```python
class QualityCheck(models.Model):
    _name = 'quality.check'
    
    workorder_id = fields.Many2one('mrp.workorder')
    production_id = fields.Many2one('mrp.production')
    
    point_id = fields.Many2one('quality.point')
    
    # Inspection results
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pass', 'Pass'),
        ('fail', 'Fail'),
        ('skip', 'Skipped')
    ])
    
    # For failed checks
    failure_message = fields.Text()
    quality_state = fields.Selection([
        ('correct', 'Correct'),
        ('reject', 'Reject'),
        ('scrap', 'Scrap'),
        ('rework', 'Rework')
    ])

class QualityPoint(models.Model):
    _name = 'quality.point'
    
    name = fields.Char()
    product_ids = fields.Many2many('product.product')
    
    # When to check
    check_execute_now = fields.Boolean()  # Check at workorder start
    
    # Check method
    test_type = fields.Selection([
        ('register_consumed_materials', 'Register Consumed Materials'),
        ('register_produced_materials', 'Register Produced Materials'),
        ('instructions', 'Instructions'),
        ('passfail', 'Pass / Fail'),
    ])
```

### **7. SCRAP & BYPRODUCTS**
```python
class MrpBomLine(models.Model):
    
    # Regular components
    product_id = fields.Many2one('product.product')
    product_qty = fields.Float()
    
    # Scrap/Byproduct
    byproduct_ids = fields.One2many('mrp.bom.byproduct', 'bom_line_id')

class MrpBomByproduct(models.Model):
    _name = 'mrp.bom.byproduct'
    
    product_id = fields.Many2one('product.product')  # Scrap product
    product_qty = fields.Float()  # Qty per unit produced
    
    # Byproduct cost recovery
    cost_share = fields.Float()  # % of raw cost attributed to byproduct

# Example: Produce 100 kg flour → 95 kg flour + 5 kg bran
# Bran is byproduct, reduces flour cost

class MrpScrap(models.Model):
    _name = 'mrp.scrap'
    
    production_id = fields.Many2one('mrp.production')
    workorder_id = fields.Many2one('mrp.workorder')
    
    product_id = fields.Many2one('product.product')  # Scrap item
    product_qty = fields.Float()
    
    # Scrap location
    location_id = fields.Many2one('stock.location')
```

### **8. SUBCONTRACTING**
```python
class MrpBom(models.Model):
    
    type = fields.Selection([
        ('normal', 'Manufacture'),
        ('phantom', 'Phantom'),
        ('subcontract', 'Subcontracting'),  # Send to vendor
    ])
    
    # For subcontracting BoMs
    subcontractor_ids = fields.Many2many(
        'res.partner',
        domain=[('is_subcontractor', '=', True)]
    )

class MrpProduction(models.Model):
    
    # If BoM type = subcontract
    def action_subcontract(self):
        """Create PO instead of production order"""
        
        # Create purchase order for subcontractor
        po = self.env['purchase.order'].create({
            'partner_id': self.bom_id.subcontractor_ids[0].id,
            'order_line': [
                (0, 0, {
                    'product_id': self.product_id.id,
                    'product_qty': self.product_qty,
                })
            ]
        })
        
        # When PO received, auto-create production receipt
        # Finished product appears in inventory from receipt
```

### **9. MASTER PRODUCTION SCHEDULE (MPS)**
```python
class MrpDemand(models.Model):
    """Forecast demand from sales/demand planning"""
    
    product_id = fields.Many2one('product.product')
    date = fields.Date()
    product_qty = fields.Float()

# MPS algorithm:
# For each product:
#   1. Get total demand (sales orders + forecast)
#   2. Get on-hand quantity
#   3. If demand > on-hand:
#      a. Calculate qty needed
#      b. Explode BoM to get component needs
#      c. Create PRs for low-stock components
#      d. Create MOs for product
```

### **10. PRODUCTION REPORTING & DASHBOARDS**
```python
class MrpProductionReport(models.TransientModel):
    """Production analytics"""
    
    @api.model
    def get_production_efficiency(self, date_from, date_to):
        """Expected vs Actual time"""
        workorders = self.env['mrp.workorder'].search([
            ('date_start', '>=', date_from),
            ('date_start', '<=', date_to),
        ])
        
        efficiency = {}
        for wo in workorders:
            expected = wo.duration_expected
            actual = wo.duration
            ratio = (expected / actual) * 100 if actual > 0 else 0
            
            efficiency[wo.workcenter_id.name] = {
                'expected': expected,
                'actual': actual,
                'efficiency': ratio,
            }
        
        return efficiency
    
    @api.model
    def get_production_cost(self, product_id, qty):
        """Estimate production cost"""
        product = self.env['product.product'].browse(product_id)
        bom = product.bom_ids[0] if product.bom_ids else None
        
        if not bom:
            return product.standard_price * qty
        
        components = bom._bom_explode(qty)
        raw_cost = sum(c['product_id'].standard_price * c['quantity'] 
                      for c in components)
        
        # Add labor estimate
        labor_cost = 0
        for op in bom.operation_ids:
            op_time = (op.time_cycle / 60) * qty  # hours
            labor_cost += op_time * op.workcenter_id.costs_hour
        
        return raw_cost + labor_cost
```

---

## 📚 **KEY MODELS CHEAT SHEET**

| Model | Purpose |
|-------|---------|
| **mrp.bom** | Bill of Materials (recipe) |
| **mrp.bom.line** | Components in BoM |
| **mrp.production** | Manufacturing order |
| **mrp.routing** | Process routing |
| **mrp.workcenter** | Work station |
| **mrp.workorder** | Actual operation |
| **quality.check** | QA check |
| **mrp.scrap** | Scrap tracking |

---

## 🔧 **COMMON TASKS**

### **Create Production Order from Sales Order**
```python
sale_order = self.env['sale.order'].browse(sale_id)
for line in sale_order.order_line:
    if line.product_id.type == 'product':
        # Check if manufactured
        bom = line.product_id.bom_ids[0]
        if bom:
            mo = self.env['mrp.production'].create({
                'product_id': line.product_id.id,
                'product_qty': line.product_uom_qty,
                'bom_id': bom.id,
            })
            mo.action_confirm()
```

### **Get Exploded BoM**
```python
bom = self.env['mrp.bom'].search([('product_id', '=', product_id)])
components = bom._bom_explode(quantity=10)
# Returns: [{'product_id': P1, 'qty': 40}, ...]
```

### **Mark Production Done & Update Inventory**
```python
mo = self.env['mrp.production'].browse(mo_id)
mo.button_mark_done()  # Finalizes all stock moves
```

---

## ⚠️ **CRITICAL RULES**

1. **BoM must exist**: Can't MO without recipe
2. **Explode recursively**: Multi-level BoMs expand to raw materials
3. **Moves locked**: Once MO confirmed, moves cannot be deleted
4. **Quality gate**: Failed QA can block move to next operation
5. **Costing**: Production cost affects inventory valuation

---

## 🚀 **QUICK REFERENCE**

- **Auto-PO**: MRP can trigger POs for low-stock components
- **Phantom BoM**: Invisible sub-assembly, expanded at production time
- **2-Step default**: Raw → Production → Finished (most flexibility)
- **Labor tracking**: Work orders capture actual time vs planned
- **Scrap/Byproducts**: Reduce finished product cost

---

**Skill Status:** ✅ Ready for all Manufacturing tasks
