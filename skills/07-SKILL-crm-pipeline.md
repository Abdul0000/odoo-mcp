# Odoo CRM & Sales Pipeline Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE PATTERNS & WORKFLOWS

### **1. LEADS & OPPORTUNITY PIPELINE**
```python
class CrmLead(models.Model):
    _name = 'crm.lead'
    
    name = fields.Char(required=True)  # Company name or contact
    type = fields.Selection([
        ('lead', 'Lead'),
        ('opportunity', 'Opportunity')
    ], default='lead')
    
    # Contact info
    partner_id = fields.Many2one('res.partner')
    email_from = fields.Char()
    phone = fields.Char()
    
    # Sales stage
    stage_id = fields.Many2one('crm.stage', required=True)
    
    # Sales team & person
    team_id = fields.Many2one('crm.team')
    user_id = fields.Many2one('res.users')  # Assigned to
    
    # Value
    expected_revenue = fields.Float()  # Potential deal size
    probability = fields.Float(default=0)  # Win % (0-100)
    
    # Timeline
    date_deadline = fields.Date()  # Expected close date
    create_date = fields.Datetime()
    
    # Description
    description = fields.Text()
    
    # Status
    active = fields.Boolean(default=True)
    lost_reason = fields.Selection([
        ('competitor', 'Chose competitor'),
        ('budget', 'Budget constraints'),
        ('timing', 'Timing not right'),
        ('unqualified', 'Not qualified'),
    ])
    
    # Activities (follow-ups)
    activity_ids = fields.One2many('mail.activity', 'res_id', 
                                    domain=[('res_model', '=', 'crm.lead')])
    
    # Converted to customer
    partner_id_final = fields.Many2one('res.partner', 'Customer')
    
    def action_convert_opportunity(self):
        """Convert lead → opportunity"""
        if self.type == 'lead':
            self.type = 'opportunity'
            self.stage_id = self.env['crm.stage'].search([
                ('sequence', '=', 1)  # First opportunity stage
            ])[0]
    
    def action_mark_as_lost(self, lost_reason):
        """Mark lead lost"""
        self.active = False
        self.lost_reason = lost_reason
    
    def action_convert_to_partner(self):
        """
        Create customer from lead
        """
        partner = self.env['res.partner'].create({
            'name': self.name,
            'email': self.email_from,
            'phone': self.phone,
        })
        
        self.partner_id_final = partner.id
        self.stage_id = self.env['crm.stage'].search([
            ('name', '=', 'Won')
        ])[0]
        
        return partner

class CrmStage(models.Model):
    _name = 'crm.stage'
    
    name = fields.Char(required=True)  # 'Qualified', 'Proposal', 'Negotiation'
    
    # Order in pipeline
    sequence = fields.Integer()
    
    # Team-specific
    team_id = fields.Many2one('crm.team')
    
    # Final stage?
    is_won = fields.Boolean()  # Is this a won stage?
    
    # Probability (for forecasting)
    probability = fields.Float()  # 0-100%
```

### **2. SALES TEAMS & STRUCTURE**
```python
class CrmTeam(models.Model):
    _name = 'crm.team'
    
    name = fields.Char(required=True)  # 'Enterprise Team'
    
    company_id = fields.Many2one('res.company')
    
    # Team lead
    user_id = fields.Many2one('res.users')
    
    # Team members
    member_ids = fields.Many2many('res.users')
    
    # Sales target
    monthly_target = fields.Float()  # Revenue target
    
    # Stages specific to team
    stage_ids = fields.One2many('crm.stage', 'team_id')
    
    # Pipeline
    lead_ids = fields.One2many('crm.lead', 'team_id')

# Sales hierarchy:
# └─ Sales Department
#    ├─ Enterprise Team (Target: $500k/mo)
#    │  ├─ Senior Rep 1 (Target: $200k/mo)
#    │  ├─ Senior Rep 2 (Target: $200k/mo)
#    │  └─ Junior Rep 1 (Target: $100k/mo)
#    ├─ SMB Team (Target: $300k/mo)
#    └─ Partnerships Team
```

### **3. LEAD SCORING & QUALIFICATION**
```python
class CrmLead(models.Model):
    
    score = fields.Float(compute='_compute_score')  # Lead quality 0-100
    
    # Lead qualification fields
    company_size = fields.Selection([
        ('1-50', 'Small'),
        ('51-200', 'Medium'),
        ('201-500', 'Large'),
        ('500+', 'Enterprise')
    ])
    
    industry = fields.Selection([
        ('tech', 'Technology'),
        ('finance', 'Financial Services'),
        ('retail', 'Retail'),
        ('manufacturing', 'Manufacturing'),
    ])
    
    budget = fields.Float()  # Customer's budget
    
    # Activity signals
    email_open_count = fields.Integer()
    email_click_count = fields.Integer()
    website_visit_count = fields.Integer()
    
    def _compute_score(self):
        """Lead scoring algorithm"""
        score = 0
        
        # Company size scoring
        if self.company_size == '500+':
            score += 30
        elif self.company_size == '201-500':
            score += 20
        elif self.company_size == '51-200':
            score += 10
        
        # Budget scoring
        if self.budget and self.budget > 100000:
            score += 25
        elif self.budget:
            score += 15
        
        # Engagement scoring
        score += min(self.email_open_count * 2, 15)  # Max 15
        score += min(self.website_visit_count, 10)  # Max 10
        
        self.score = score
    
    def is_qualified(self):
        """Is this lead sales-ready?"""
        return self.score >= 50 and self.budget and self.company_size
```

### **4. ACTIVITIES & FOLLOW-UPS**
```python
class MailActivity(models.Model):
    _name = 'mail.activity'
    
    res_model = fields.Char()  # 'crm.lead'
    res_id = fields.Integer()  # Lead ID
    
    activity_type_id = fields.Many2one('mail.activity.type')
    # Types: Call, Email, Meeting, Demo, Proposal
    
    summary = fields.Char()  # 'Follow up on pricing'
    description = fields.Text()
    
    # Schedule
    date_deadline = fields.Date()  # When to do this activity
    
    # Assignment
    user_id = fields.Many2one('res.users')
    
    state = fields.Selection([
        ('today', 'Today'),
        ('overdue', 'Overdue'),
        ('planned', 'Planned')
    ], compute='_compute_state')
    
    # Completion
    done = fields.Boolean()
    
    def action_mark_done(self):
        """Complete activity"""
        self.done = True
        
        # Suggest next activity
        # E.g., if 'Call' done, suggest 'Send Proposal' in 2 days

class MailActivityType(models.Model):
    _name = 'mail.activity.type'
    
    name = fields.Char()  # 'Phone Call'
    summary = fields.Char()  # Default summary template
    default_user_type = fields.Selection([
        ('owner', 'Owner'),
        ('team_lead', 'Team Lead'),
    ])
```

### **5. SALES FORECAST & PIPELINE REPORTS**
```python
class CrmLead(models.Model):
    
    # Weighted revenue (for forecasting)
    weighted_revenue = fields.Float(
        compute='_compute_weighted'
    )  # expected_revenue * (probability/100)
    
    def _compute_weighted(self):
        """For pipeline forecast"""
        self.weighted_revenue = (self.expected_revenue or 0) * (self.probability or 0) / 100

class CrmpipelineReport(models.TransientModel):
    """Sales pipeline analytics"""
    
    @api.model
    def get_pipeline_by_stage(self, team_id=None):
        """
        Pipeline by stage:
        Stage | Count | Total Value | Weighted Value | Avg Deal Size
        Qualified | 5 | $500k | $250k (50% prob) | $100k
        Proposal | 3 | $300k | $240k (80% prob) | $100k
        Negotiation | 2 | $200k | $160k (80% prob) | $100k
        """
        query = [('type', '=', 'opportunity')]
        if team_id:
            query.append(('team_id', '=', team_id))
        
        leads = self.env['crm.lead'].search(query)
        
        pipeline = {}
        for stage in self.env['crm.stage'].search([]):
            stage_leads = leads.filtered(lambda l: l.stage_id == stage)
            
            pipeline[stage.name] = {
                'count': len(stage_leads),
                'total_value': sum(l.expected_revenue or 0 for l in stage_leads),
                'weighted_value': sum(l.weighted_revenue or 0 for l in stage_leads),
                'avg_deal': sum(l.expected_revenue or 0 for l in stage_leads) / len(stage_leads) if stage_leads else 0,
            }
        
        return pipeline
    
    @api.model
    def get_forecast_accuracy(self, month):
        """How accurate were our forecasts?"""
        # Compare forecasted revenue vs actual closed deals
        pass
    
    @api.model
    def get_win_rate(self, team_id=None):
        """Win rate %"""
        query = [('type', '=', 'opportunity')]
        if team_id:
            query.append(('team_id', '=', team_id))
        
        all_opps = self.env['crm.lead'].search(query)
        won = all_opps.filtered(lambda l: l.stage_id.is_won)
        
        return (len(won) / len(all_opps) * 100) if all_opps else 0
    
    @api.model
    def get_avg_deal_cycle(self, team_id=None):
        """Average days from lead to close"""
        # Won deals
        query = [('type', '=', 'opportunity'), ('stage_id.is_won', '=', True)]
        if team_id:
            query.append(('team_id', '=', team_id))
        
        won_deals = self.env['crm.lead'].search(query)
        
        cycles = []
        for deal in won_deals:
            cycle = (deal.date_closed - deal.create_date).days
            cycles.append(cycle)
        
        avg_cycle = sum(cycles) / len(cycles) if cycles else 0
        return avg_cycle
```

### **6. LEAD SOURCES & ATTRIBUTION**
```python
class CrmLead(models.Model):
    
    source_id = fields.Many2one('crm.lead.source')
    # Sources: Website, LinkedIn, Referral, Cold Call, Trade Show, etc.
    
    # UTM tracking
    campaign_id = fields.Many2one('utm.campaign')  # 'Summer 2024'
    medium = fields.Char()  # 'email', 'organic', 'paid'
    source = fields.Char()  # 'google', 'facebook', 'newsletter'

class CrmLeadSource(models.Model):
    _name = 'crm.lead.source'
    
    name = fields.Char()  # 'Website Contact Form'
    
    # Tracking
    conversion_rate = fields.Float(compute='_compute_conversion')
    # % of leads from this source that convert to opportunities

class UtmCampaign(models.Model):
    _name = 'utm.campaign'
    
    name = fields.Char()  # 'Summer Sale 2024'
    
    # Budget
    budget = fields.Float()
    spend = fields.Float(compute='_compute_spend')
    
    # Results
    lead_ids = fields.One2many('crm.lead', 'campaign_id')
    
    # ROI
    roi = fields.Float(compute='_compute_roi')  # Revenue / Spend
```

### **7. SALES CHANNEL INTEGRATION**
```python
# Import leads from various sources:

# Email → Create lead
class MailAlias(models.Model):
    alias_model_name = fields.Char()  # = 'crm.lead'
    alias_name = fields.Char()  # = 'sales'
    # Email: sales@mycompany.com → Creates lead

# Website form → Lead
class WebsiteContactUs(controllers.Controller):
    
    @http.route('/contact-us/submit', type='http', methods=['POST'])
    def submit_contact(self, **post):
        """Create lead from website form"""
        lead = request.env['crm.lead'].create({
            'name': post.get('name'),
            'email_from': post.get('email'),
            'phone': post.get('phone'),
            'description': post.get('message'),
        })
        return lead

# LinkedIn → Lead (via API integration)
```

### **8. SALES AUTOMATION & WORKFLOWS**
```python
"""
Automation rules: Auto-assign, auto-follow-up, etc.
"""

class CrmAutomationRule(models.Model):
    _name = 'crm.automation.rule'
    
    name = fields.Char()  # 'Auto-qualify high-budget leads'
    
    # Trigger
    trigger_type = fields.Selection([
        ('create', 'When lead created'),
        ('update', 'When lead updated'),
        ('state_change', 'When stage changed'),
    ])
    
    # Condition
    condition = fields.Text()  # Python eval: lead.budget > 100000
    
    # Action
    action_type = fields.Selection([
        ('assign', 'Assign to user'),
        ('stage', 'Move to stage'),
        ('activity', 'Create activity'),
        ('tag', 'Add tag'),
    ])
    
    target_user_id = fields.Many2one('res.users')  # For assign action
    target_stage_id = fields.Many2one('crm.stage')  # For stage action
    
    def apply_rule(self, lead):
        """Apply automation rule to lead"""
        if eval(self.condition, {'lead': lead}):
            if self.action_type == 'assign':
                lead.user_id = self.target_user_id.id
            elif self.action_type == 'stage':
                lead.stage_id = self.target_stage_id.id
            elif self.action_type == 'activity':
                self.env['mail.activity'].create({
                    'res_model': 'crm.lead',
                    'res_id': lead.id,
                    'summary': self.name,
                    'user_id': self.target_user_id.id,
                })
```

### **9. PREDICTIVE ANALYTICS**
```python
class CrmLead(models.Model):
    
    # ML prediction
    churn_probability = fields.Float()  # % chance lead won't close
    
    # Suggested next action (AI)
    ai_suggested_action = fields.Char()  # 'Send demo email'
    ai_confidence = fields.Float()  # 0-100%
```

### **10. KANBAN PIPELINE VIEW**
```python
# CRM module renders pipeline as Kanban board:
# 
# [ Lead Qualification ]  [ Proposal Sent ]  [ Negotiation ]  [ Won ]
#  ___________________     ______________    ______________  ______
# | Lead 1: $50k      |   | Opp 5: $100k |  | Opp 8: $75k | Deal 12
# |  Score: 75        |   |  Prob: 70%   |  |  Prob: 85%  | $250k
# |___________________|   |______________|  |_____________|________
# | Lead 2: $80k      |   | Opp 6: $120k |
# |  Score: 60        |   |  Prob: 60%   |
# |___________________|   |______________|

# Drag & drop to move between stages
# Color-coded by probability/value
```

---

## 📚 **KEY MODELS CHEAT SHEET**

| Model | Purpose |
|-------|---------|
| **crm.lead** | Lead/opportunity |
| **crm.stage** | Pipeline stage |
| **crm.team** | Sales team |
| **mail.activity** | Follow-up task |
| **utm.campaign** | Marketing campaign |
| **crm.lead.source** | Lead origin |

---

## 🔧 **COMMON TASKS**

### **Create Lead**
```python
lead = self.env['crm.lead'].create({
    'name': 'ABC Corp',
    'email_from': 'contact@abccorp.com',
    'expected_revenue': 50000,
    'team_id': team.id,
})
```

### **Convert to Opportunity**
```python
lead.action_convert_opportunity()
```

### **Get Pipeline**
```python
report = self.env['crm.pipeline.report']
pipeline = report.get_pipeline_by_stage(team_id)
# Returns: {
#   'Qualified': {'count': 5, 'total': 500k, 'weighted': 250k},
#   ...
# }
```

---

## ⚠️ **CRITICAL RULES**

1. **Lead vs Opportunity**: Lead = prospect, Opportunity = qualified lead
2. **Probability**: Affects forecast (weighted revenue)
3. **Stage progression**: Lead moves through defined stages
4. **Assignment**: Each opp assigned to salesperson
5. **Activities**: Track all customer interactions

---

**Skill Status:** ✅ Ready for all CRM & Sales Pipeline tasks
