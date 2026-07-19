---
name: odoo-website-development
description: Build Odoo websites (v16-19) with custom themes, QWeb templates, building blocks, eCommerce config, and module customization. Short, powerful reference for theme development, product management, SEO, and performance.
---

# Odoo Website Development

## Theme Module (5 Min Setup)

**Directory Structure**
```
website_theme_custom/
  ├── __manifest__.py
  ├── views/website_templates.xml
  ├── views/building_blocks.xml
  ├── static/src/css/custom.scss
  ├── static/src/js/custom.js
  └── data/website_data.xml
```

**__manifest__.py**
```python
{
    'name': 'Custom Theme',
    'depends': ['website', 'website_sale'],
    'data': ['views/website_templates.xml', 'data/website_data.xml'],
    'assets': {
        'web._assets_primary_variables': ['website_theme_custom/static/src/css/custom.scss'],
        'web.assets_frontend': ['website_theme_custom/static/src/js/custom.js'],
    },
}
```

## QWeb Templates (Inheritance Pattern)

**Override Header**
```xml
<!-- views/website_templates.xml -->
<template id="website.layout" inherit_id="website.layout">
  <xpath expr="//header" position="replace">
    <header class="navbar navbar-light bg-light">
      <div class="container">
        <span class="navbar-brand">
          <img t-attf-src="/web/image/website/{website.id}/logo" 
               alt="Logo" style="max-height: 40px;"/>
        </span>
        <nav class="navbar-nav">
          <t t-foreach="website.menu_id.child_id" t-as="menu">
            <a t-attf-href="{menu.url}" class="nav-link"><t t-esc="menu.name"/></a>
          </t>
        </nav>
      </div>
    </header>
  </xpath>
</template>
```

## Building Blocks (Snippets)

**Create Custom Block**
```xml
<!-- views/building_blocks.xml -->
<template id="s_feature_box" name="Feature Box">
  <section class="s_feature_box pt-5 pb-5">
    <div class="container">
      <div class="row">
        <div class="col-lg-6">
          <h3>Title</h3>
          <p>Description editable in website builder.</p>
        </div>
        <div class="col-lg-6">
          <img src="/website_theme_custom/static/src/img/feature.svg" class="img-fluid"/>
        </div>
      </div>
    </div>
  </section>
</template>
```

**Make Editable in Builder**
```xml
<template id="s_feature_box_options" inherit_id="web_editor.snippet_options">
  <xpath expr="." position="inside">
    <div data-js="Feature" data-selector="section.s_feature_box">
      <!-- Inspector options auto-generated -->
    </div>
  </xpath>
</template>
```

## Styling (SCSS Variables)

```scss
/* static/src/css/custom.scss */
:root {
  --primary: #0066cc;
  --secondary: #ff6b35;
  --font-family: 'Segoe UI', sans-serif;
}

body {
  font-family: var(--font-family);
}

a { color: var(--primary); }
.btn-primary { background: var(--primary); }

@media (max-width: 768px) {
  h1 { font-size: 28px; }
}
```

## JavaScript Interactivity

```javascript
// static/src/js/custom.js
document.addEventListener('DOMContentLoaded', () => {
  // Smooth scroll
  document.querySelectorAll('a[href^="#"]').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      document.querySelector(link.getAttribute('href'))
        ?.scrollIntoView({ behavior: 'smooth' });
    });
  });

  // Lazy load images
  new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.src = entry.target.dataset.src;
      }
    });
  }).observe(document.querySelectorAll('img[data-src]'));
});
```

## eCommerce (Product Page)

**Product Display**
```xml
<template id="product_page">
  <section id="product-details">
    <div class="row">
      <div class="col-md-6">
        <img t-attf-src="/web/image/product.product/{product.id}/image_1024" class="img-fluid"/>
      </div>
      <div class="col-md-6">
        <h1 t-esc="product.name"/>
        <p class="price">PKR <t t-esc="product.list_price"/></p>
        <form method="post" action="/shop/cart/update">
          <input type="hidden" name="product_id" t-attf-value="{product.id}"/>
          <input type="number" name="quantity" value="1" min="1"/>
          <button type="submit" class="btn btn-primary">Add to Cart</button>
        </form>
        <p t-esc="product.description_sale"/>
      </div>
    </div>
  </section>
</template>
```

**Product Model Extension**
```python
# models/product_product.py
from odoo import models, fields

class ProductProduct(models.Model):
    _inherit = 'product.product'
    
    rating = fields.Float('Rating', default=0.0)
    review_count = fields.Integer('Reviews', compute='_compute_reviews')
    
    def _compute_reviews(self):
        for p in self:
            p.review_count = self.env['product.review'].search_count([('product_id', '=', p.id)])
```

## Custom Controller (Forms & Routes)

```python
# controllers/website.py
from odoo import http
from odoo.http import request

class WebsiteController(http.Controller):
    
    @http.route('/contact/submit', type='json', auth='public', website=True)
    def contact_submit(self, name, email, message, **kw):
        request.env['crm.lead'].create({
            'name': name,
            'email_from': email,
            'description': message,
        })
        return {'status': 'success', 'message': 'Message sent!'}
    
    @http.route('/products/filter', type='http', auth='public', website=True)
    def filter_products(self, category=None, min_price=0, **kw):
        domain = [('website_published', '=', True)]
        if category:
            domain.append(('categ_id', '=', int(category)))
        if min_price:
            domain.append(('list_price', '>=', float(min_price)))
        
        products = request.env['product.product'].search(domain)
        return request.render('website_theme_custom.filtered_products', {
            'products': products
        })
```

## Multi-Language & SEO

**Language Setup**
```python
# In Website settings
# Settings → Languages → Add: en_US, fr_FR, es_ES
# Pages auto-translate: /en/page, /fr/page
```

**Meta Tags**
```xml
<meta property="og:title" t-attf-content="{page.name}"/>
<meta property="og:description" t-attf-content="{page.meta_description}"/>
<meta name="robots" t-attf-content="{page.robots}"/>
```

**Structured Data (JSON-LD)**
```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Product Name",
  "price": "29.99",
  "aggregateRating": {"ratingValue": "4.5", "reviewCount": "100"}
}
</script>
```

## Performance Tips

**Lazy Load Images**
```html
<img src="/placeholder.png" data-src="/actual-image.jpg" loading="lazy"/>
```

**Query Optimization**
```python
# Use prefetch for many2many
products = request.env['product.product'].search(
    [('website_published', '=', True)]
).with_prefetch('categ_id', 'product_template_attribute_value_ids')
```

**Asset Bundling** (auto-minified in production)
```python
# Define in assets → web.assets_frontend
# Odoo bundles & caches CSS/JS automatically
```

## Common Patterns

| Task | Code |
|------|------|
| Get current website | `website = request.website` |
| Check if user loggedin | `if request.env.user.id != public_user_id:` |
| Get domain URL | `website.domain` or `request.httprequest.host` |
| Inherit a template | `<template inherit_id="website.page">` |
| Make form field required | `<input required/>` or `fields.Char(required=True)` |
| Add CSS class conditionally | `t-attf-class="row {'' if product else 'd-none'}"` |

## Multi-Site Setup

```python
# Website context (auto-detected)
website = request.website  # Current website object
# Each website can have:
# - Unique domain, logo, colors
# - Separate product catalog (with website_id filter)
# - Own menus, pages, blog

# Query products for current website
products = request.env['product.product'].search([
    ('website_published', '=', True),
    ('website_id', '=', website.id),
])
```

## Version Notes (v16-19)

| Feature | v16 | v17+ | v18+ | v19+ |
|---------|-----|------|------|------|
| website_ecommerce | Separate | website_sale | website_sale | website_sale |
| OWL components | Basic | Enhanced | Enhanced | Enhanced |
| SEO tools | Basic | Improved | Improved | + AI suggestions |
| Building blocks | Full | Full | Full | Expanded library |

## Deployment Checklist

```
✓ manifest.py dependencies correct
✓ XML valid (xmllint views/*.xml)
✓ SCSS compiles (no errors in browser console)
✓ JS console clear (no 404s for assets)
✓ Migrate: odoo-bin -u module_name -d db_name
✓ Website published & responsive
✓ Meta tags populated (title, description)
✓ Images optimized (<100KB per image)
✓ Analytics tracking installed
✓ Backup before production deploy
```

## Quick Refs

**Common Paths**
- Pages: Website → Pages
- eCommerce: Sales → Products
- Menus: Website → Menus
- Settings: Website → Settings
- Analytics: Website → Reporting

**File Access**
- `/web/image/website/{id}/logo`
- `/web/image/product.product/{id}/image_1024`
- `/website_theme_custom/static/src/img/file.svg`

**ORM Shortcuts**
```python
# Search with defaults
env['model'].search([('field', '=', value)])

# Get one or error
env['model'].search([...], limit=1)[0]

# Count
env['model'].search_count([...])

# Browse by ID
env['model'].browse(id)

# Create
env['model'].create({'field': 'value'})
```