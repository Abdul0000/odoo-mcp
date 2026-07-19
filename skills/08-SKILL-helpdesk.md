# Odoo Helpdesk & Support Development Skill

**Version:** Odoo 16-19 | **Complexity:** ⭐⭐⭐ | **Last Updated:** 2024

---

## 🎯 CORE PATTERNS & WORKFLOWS

### **1. HELPDESK TICKETS**
```python
class HelpdeskTicket(models.Model):
    _name = 'helpdesk.ticket'
    
    name = fields.Char(auto=True)  # TKT-2024-001
    
    # Customer info
    partner_id = fields.Many2one('res.partner', required=True)
    partner_name = fields.Char()
    partner_email = fields.Char()
    
    # Ticket details
    description = fields.Text(required=True)  # Issue description
    
    # Category
    category_id = fields.Many2one('helpdesk.category')
    # Types: Bug, Feature Request, Account Issue, Billing, etc.
    
    # Priority
    priority = fields.Selection([
        ('0', 'Low'),
        ('1', 'Normal'),
        ('2', 'High'),
        ('3', 'Urgent')
    ], default='1')
    
    # Status
    stage_id = fields.Many2one('helpdesk.stage')  # Open, In Progress, Resolved
    
    # Assignment
    user_id = fields.Many2one('res.users')  # Assigned agent
    team_id = fields.Many2one('helpdesk.team')
    
    # Timeline
    create_date = fields.Datetime()  # When reported
    date_deadline = fields.Date()  # SLA deadline
    close_date = fields.Datetime()  # When resolved
    
    # Resolution
    resolution = fields.Text()  # How it was fixed
    
    # Tracking
    ticket_ref = fields.Char()  # External ticket ID
    tag_ids = fields.Many2many('helpdesk.tag')
    
    # Escalation
    is_escalated = fields.Boolean()
    escalated_to = fields.Many2one('res.users')  # Escalated agent
    
    # Customer satisfaction
    rating = fields.Float()  # 1-5 stars
    feedback = fields.Text()
    
    state = fields.Selection([
        ('open', 'Open'),
        ('pending', 'Pending Customer Input'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('closed', 'Closed'),
        ('cancelled', 'Cancelled')
    ])

# WORKFLOW TRANSITIONS
def action_open(self):
    """Ticket opened"""
    self.state = 'open'
    self.create_date = fields.Datetime.now()
    
    # Calculate SLA deadline
    sla = self.team_id.sla_ids[0] if self.team_id.sla_ids else None
    if sla:
        self.date_deadline = fields.Date.add(
            fields.Date.today(),
            days=sla.response_days
        )

def action_start(self):
    """Agent starts working on ticket"""
    self.state = 'in_progress'

def action_pending(self):
    """Waiting for customer input"""
    self.state = 'pending'

def action_done(self):
    """Mark ticket resolved"""
    self.state = 'done'
    self.close_date = fields.Datetime.now()
    
    # Calculate resolution time
    resolution_hours = (
        self.close_date - self.create_date
    ).total_seconds() / 3600

def action_close(self):
    """Close ticket (no further responses)"""
    self.state = 'closed'

def action_cancel(self):
    """Cancel ticket"""
    self.state = 'cancelled'

def action_escalate(self, escalate_to_user):
    """Escalate to senior agent"""
    self.is_escalated = True
    self.escalated_to = escalate_to_user
```

### **2. HELPDESK TEAMS & CONFIGURATION**
```python
class HelpdeskTeam(models.Model):
    _name = 'helpdesk.team'
    
    name = fields.Char(required=True)  # 'Support Team', 'Billing'
    
    # Team lead
    user_id = fields.Many2one('res.users')
    
    # Members
    member_ids = fields.Many2many('res.users')
    
    # Ticket assignment
    assignment_method = fields.Selection([
        ('manual', 'Manually assigned'),
        ('random', 'Random'),
        ('least_busy', 'Least busy agent'),
        ('round_robin', 'Round robin'),
    ])
    
    # Auto-assignment
    auto_assign = fields.Boolean(default=True)
    
    # SLAs
    sla_ids = fields.One2many('helpdesk.sla', 'team_id')
    
    # Kanban stages
    stage_ids = fields.Many2many('helpdesk.stage')
    
    # Default stage
    default_stage_id = fields.Many2one('helpdesk.stage')
    
    # Ticket source
    source_ids = fields.Many2many('helpdesk.ticket.source')
    
    # Portal
    portal_enabled = fields.Boolean(default=True)

class HelpdeskStage(models.Model):
    _name = 'helpdesk.stage'
    
    name = fields.Char()  # 'New', 'In Progress', 'Resolved'
    sequence = fields.Integer()  # Order
    
    # Final stage?
    is_closed = fields.Boolean()  # Cannot move back if true
    
    # Folded (hidden by default)
    fold = fields.Boolean()

class HelpdeskTicketSource(models.Model):
    _name = 'helpdesk.ticket.source'
    
    name = fields.Char()  # 'Email', 'Phone', 'Chat', 'Portal'
    
    channel = fields.Selection([
        ('email', 'Email'),
        ('phone', 'Phone'),
        ('website', 'Website'),
        ('chat', 'Live Chat'),
        ('social', 'Social Media'),
    ])
```

### **3. SLA (SERVICE LEVEL AGREEMENT)**
```python
class HelpdeskSla(models.Model):
    _name = 'helpdesk.sla'
    
    name = fields.Char(required=True)  # 'Urgent - 4 hour response'
    team_id = fields.Many2one('helpdesk.team')
    
    # Trigger
    priority = fields.Selection([
        ('0', 'Low'),
        ('1', 'Normal'),
        ('2', 'High'),
        ('3', 'Urgent')
    ])
    
    # Response & Resolution times
    response_days = fields.Float()  # Hours to first response
    resolution_days = fields.Float()  # Hours to close
    
    # Auto-escalation
    auto_escalate = fields.Boolean()
    escalation_days = fields.Float()  # Escalate after X hours
    
    # Email notification
    notify_on_breach = fields.Boolean(default=True)

# SLA TRIGGERS
def check_sla_breach(self):
    """Check if ticket breached SLA"""
    sla = self.team_id.sla_ids.filtered(
        lambda s: s.priority == self.priority
    )[0] if self.team_id.sla_ids else None
    
    if not sla:
        return False
    
    # Check response time
    if not self.user_id:  # Not yet assigned
        time_elapsed = (
            fields.Datetime.now() - self.create_date
        ).total_seconds() / 3600
        
        if time_elapsed > sla.response_days:
            self.sla_breached = True
            if sla.notify_on_breach:
                self.send_sla_alert()
            
            if sla.auto_escalate:
                self.action_escalate()
    
    return self.sla_breached

def send_sla_alert(self):
    """Notify team lead of SLA breach"""
    # Send email to team lead
    pass
```

### **4. TICKET COMMUNICATION & MESSAGES**
```python
class HelpdeskTicket(models.Model):
    
    # Messages (emails/comments)
    message_ids = fields.One2many('mail.message', 'res_id',
                                  domain=[('res_model', '=', 'helpdesk.ticket')])
    
    # Internal notes (visible to team only)
    internal_notes = fields.Text()
    
    def send_customer_reply(self, message_text, attachments=None):
        """Send reply to customer"""
        # Create message
        self.env['mail.message'].create({
            'body': message_text,
            'model': 'helpdesk.ticket',
            'res_id': self.id,
            'message_type': 'comment',
            'author_id': self.env.user.partner_id.id,
            'attachment_ids': attachments or [],
        })
        
        # Email customer
        email_template = self.env.ref('helpdesk.email_ticket_reply')
        email_template.send_mail(self.id)

def add_internal_note(self, note):
    """Add internal note (team only)"""
    self.internal_notes += f"\n{fields.Datetime.now()}: {note}"
```

### **5. KNOWLEDGE BASE & FAQ**
```python
class HelpdeskKnowledgeBase(models.Model):
    _name = 'helpdesk.knowledge'
    
    name = fields.Char(required=True)  # Article title
    
    # Content
    body = fields.Html()  # Article text
    
    # Category
    category_id = fields.Many2one('helpdesk.knowledge.category')
    
    # Tags
    tag_ids = fields.Many2many('helpdesk.tag')
    
    # Visibility
    published = fields.Boolean(default=False)
    portal_visible = fields.Boolean(default=True)
    
    # Tracking
    view_count = fields.Integer()
    rating = fields.Float(compute='_compute_rating')  # Average rating
    
    # Help indicator
    is_faq = fields.Boolean()  # Frequently asked?
    
    def action_publish(self):
        """Make visible to customers"""
        self.published = True

class HelpdeskKnowledgeCategory(models.Model):
    _name = 'helpdesk.knowledge.category'
    
    name = fields.Char()  # 'Getting Started', 'Troubleshooting'
    
    sequence = fields.Integer()
    
    # Parent category
    parent_id = fields.Many2one('helpdesk.knowledge.category')

class KnowledgeArticleRating(models.Model):
    _name = 'helpdesk.knowledge.rating'
    
    knowledge_id = fields.Many2one('helpdesk.knowledge')
    
    rating = fields.Float()  # 1-5
    comment = fields.Text()
    user_id = fields.Many2one('res.users')
    
    helpful = fields.Boolean()  # Was this helpful?

# Self-service
class HelpdeskTicket(models.Model):
    
    # Suggested KB articles
    suggested_knowledge_ids = fields.Many2many(
        'helpdesk.knowledge',
        compute='_compute_suggested_kb'
    )
    
    def _compute_suggested_kb(self):
        """Suggest KB articles based on ticket keywords"""
        # Search KB by tags or keyword match
        keywords = self.description.split()
        
        articles = self.env['helpdesk.knowledge'].search([
            ('published', '=', True),
            '|', '|',
            ('body', 'ilike', ' '.join(keywords[:3])),
            ('name', 'ilike', ' '.join(keywords[:3])),
            ('tag_ids.name', 'in', keywords),
        ])
        
        self.suggested_knowledge_ids = articles[:5]
```

### **6. TICKET AUTOMATION & ROUTING**
```python
class HelpdeskAutomation(models.Model):
    _name = 'helpdesk.automation'
    
    name = fields.Char()  # 'Auto-assign urgent tickets'
    
    # Trigger
    trigger_type = fields.Selection([
        ('create', 'When ticket created'),
        ('assign', 'When assigned'),
        ('stage_change', 'When stage changed'),
    ])
    
    # Condition
    priority = fields.Selection([('0', 'Low'), ('1', 'Normal'), ('2', 'High'), ('3', 'Urgent')])
    tag_ids = fields.Many2many('helpdesk.tag')
    category_id = fields.Many2one('helpdesk.category')
    
    # Action
    action_type = fields.Selection([
        ('assign', 'Assign to user'),
        ('escalate', 'Escalate'),
        ('close', 'Close ticket'),
        ('notify', 'Send notification'),
        ('tag', 'Add tag'),
    ])
    
    target_user_id = fields.Many2one('res.users')
    
    def apply_rule(self, ticket):
        """Auto-apply routing rule"""
        if self.priority and ticket.priority != self.priority:
            return  # Condition not met
        
        if self.action_type == 'assign':
            ticket.user_id = self.target_user_id.id
        elif self.action_type == 'escalate':
            ticket.action_escalate(self.target_user_id)
        elif self.action_type == 'notify':
            # Send email to team
            pass

# AUTO-ROUTING EXAMPLE:
# Rule: If Urgent → Assign to senior agent
# Rule: If Billing + Low Priority → Close with canned response
```

### **7. CANNED RESPONSES & TEMPLATES**
```python
class HelpdeskCannedResponse(models.Model):
    _name = 'helpdesk.canned.response'
    
    name = fields.Char(required=True)  # 'Password Reset Instructions'
    
    team_id = fields.Many2one('helpdesk.team')
    
    # Content (with placeholders)
    text = fields.Text()
    # Placeholders: $customer_name, $ticket_ref, $date
    
    # Shortcut
    shortcut = fields.Char()  # Type '/pass' to insert
    
    category_id = fields.Many2one('helpdesk.canned.response.category')
    
    def render(self, ticket):
        """Replace placeholders with actual values"""
        text = self.text
        text = text.replace('$customer_name', ticket.partner_id.name)
        text = text.replace('$ticket_ref', ticket.name)
        text = text.replace('$date', str(fields.Date.today()))
        
        return text

# Agent can quickly reply:
# Agent types: "Please follow these steps: /pass_reset"
# → Automatically expands to full reset instructions
```

### **8. TICKET MERGING & SPLIT**
```python
class HelpdeskTicket(models.Model):
    
    # Related/duplicate tickets
    related_ticket_ids = fields.Many2many('helpdesk.ticket')
    is_duplicate = fields.Boolean()
    merged_with_id = fields.Many2one('helpdesk.ticket')
    
    def action_merge_tickets(self, other_ticket_ids):
        """Merge multiple tickets into one"""
        # Combine all messages
        for ticket in other_ticket_ids:
            for msg in ticket.message_ids:
                msg.res_id = self.id
            
            # Mark as merged
            ticket.merged_with_id = self.id
    
    def action_split_ticket(self, split_messages):
        """Split ticket into separate ticket"""
        new_ticket = self.env['helpdesk.ticket'].create({
            'partner_id': self.partner_id.id,
            'description': f"Split from {self.name}",
            'team_id': self.team_id.id,
        })
        
        # Move selected messages
        for msg in split_messages:
            msg.res_id = new_ticket.id
        
        return new_ticket
```

### **9. TICKET SURVEYS & SATISFACTION**
```python
class HelpdeskSurvey(models.Model):
    _name = 'helpdesk.survey'
    
    name = fields.Char()  # 'Post-Ticket Survey'
    
    # Questions
    question_ids = fields.One2many('helpdesk.survey.question', 'survey_id')

class HelpdeskSurveyQuestion(models.Model):
    _name = 'helpdesk.survey.question'
    
    survey_id = fields.Many2one('helpdesk.survey')
    
    question = fields.Char()
    
    question_type = fields.Selection([
        ('rating', 'Rating 1-5'),
        ('nps', 'NPS (0-10)'),
        ('choice', 'Multiple choice'),
        ('text', 'Open text'),
    ])

class HelpdeskTicket(models.Model):
    
    # Survey sent after ticket close
    survey_sent = fields.Boolean()
    
    rating = fields.Float()  # 1-5 stars
    rating_reason = fields.Text()
    nps = fields.Float()  # Net Promoter Score (0-10)
    
    def action_send_survey(self):
        """Send satisfaction survey to customer"""
        email_template = self.env.ref('helpdesk.email_ticket_survey')
        email_template.send_mail(self.id)
        self.survey_sent = True
```

### **10. HELPDESK ANALYTICS & REPORTING**
```python
class HelpdeskReport(models.TransientModel):
    """Helpdesk analytics"""
    
    @api.model
    def get_team_metrics(self, team_id, date_from, date_to):
        """Team performance metrics"""
        tickets = self.env['helpdesk.ticket'].search([
            ('team_id', '=', team_id),
            ('create_date', '>=', date_from),
            ('create_date', '<=', date_to),
        ])
        
        closed = tickets.filtered(lambda t: t.state in ['closed', 'done'])
        
        return {
            'total_tickets': len(tickets),
            'closed_tickets': len(closed),
            'open_tickets': len(tickets) - len(closed),
            'avg_rating': sum(t.rating or 0 for t in closed) / len(closed) if closed else 0,
            'avg_resolution_time': self._compute_avg_resolution(closed),
            'sla_compliance': self._compute_sla_compliance(closed),
        }
    
    @api.model
    def get_agent_performance(self, user_id, date_from, date_to):
        """Individual agent metrics"""
        tickets = self.env['helpdesk.ticket'].search([
            ('user_id', '=', user_id),
            ('create_date', '>=', date_from),
            ('create_date', '<=', date_to),
        ])
        
        closed = tickets.filtered(lambda t: t.state in ['closed', 'done'])
        
        return {
            'tickets_handled': len(tickets),
            'tickets_closed': len(closed),
            'avg_rating': sum(t.rating or 0 for t in closed) / len(closed) if closed else 0,
            'first_contact_resolution': len(
                closed.filtered(lambda t: len(t.message_ids) <= 3)
            ) / len(closed) * 100 if closed else 0,
        }
    
    @api.model
    def get_category_metrics(self, category_id):
        """Metrics by ticket category"""
        tickets = self.env['helpdesk.ticket'].search([
            ('category_id', '=', category_id),
        ])
        
        return {
            'total': len(tickets),
            'avg_resolution_time': self._compute_avg_resolution(tickets),
            'trending_issues': self._get_trending_tags(tickets),
        }
```

---

## 📚 **KEY MODELS CHEAT SHEET**

| Model | Purpose |
|-------|---------|
| **helpdesk.ticket** | Support ticket |
| **helpdesk.team** | Support team |
| **helpdesk.sla** | Service level |
| **helpdesk.knowledge** | FAQ/KB article |
| **helpdesk.canned.response** | Template reply |
| **helpdesk.survey** | Satisfaction survey |

---

## 🔧 **COMMON TASKS**

### **Create Ticket**
```python
ticket = self.env['helpdesk.ticket'].create({
    'partner_id': customer.id,
    'description': 'Cannot reset password',
    'priority': '2',
    'category_id': category.id,
})
ticket.action_open()
```

### **Assign Ticket**
```python
ticket.user_id = agent.id
ticket.action_start()
```

### **Send Reply**
```python
ticket.send_customer_reply('Your issue is resolved. Please restart the app.')
```

### **Close Ticket**
```python
ticket.action_done()
ticket.action_close()
```

---

## ⚠️ **CRITICAL RULES**

1. **SLA tracking**: Track response & resolution time
2. **Assignment**: Each ticket assigned to agent
3. **Escalation**: Auto-escalate on SLA breach
4. **Portal access**: Customer can view ticket status
5. **Knowledge linking**: Suggest KB articles to customer

---

**Skill Status:** ✅ Ready for all Helpdesk & Support tasks
