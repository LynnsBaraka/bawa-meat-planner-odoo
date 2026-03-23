{
    'name': 'Bawa Meat Planner',
    'version': '18.0.2.0.0',
    'category': 'Manufacturing/Butchery',
    'summary': 'Enterprise butchery planning engine with thick server architecture using Python ORM',
    'description': """
        Bawa Meat Planner: Evergreen Dynamic Harvest
        =============================================
        Enterprise grade production planning for beef butchery.
        All planning logic runs server side in Python using Odoo ORM.
        Supports CRON scheduling, external API access, and unlimited inventory scale.

        Features:
        - 5 level product hierarchy (Quarters > Primals > End Cuts > Value Add > Processed)
        - Demand driven reverse explosion with FEFO inventory netting
        - Avalanche trim projection from quarter processing
        - FEFO + cost substitution optimizer with source specific yields
        - Recursive Driver for auto scaling quarter procurement
        - Supply driven forward explosion (carcass / quarter / primal input modes)
        - Transient wizard for interactive What If staging
        - Real mrp.production generation from committed plans
        - CRON schedulable plan generation
    """,
    'author': 'Baraka Waswa: Odoo Business Analyst',
    'website': 'https://meat-planner.vercel.app',
    'depends': [
        'stock',
        'mrp',
        'sale',
        'sale_management',
        'product',
        'product_expiry',
        'mail',
        'web',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/bawa_yield_template_data.xml',
        'views/menus.xml',
        'views/bawa_yield_template_views.xml',
        'views/bawa_plan_views.xml',
        'views/bawa_product_extension_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'bawa_meat_planner/static/src/components/planning_engine/planning_engine.js',
            'bawa_meat_planner/static/src/components/planning_engine/planning_engine.xml',
            'bawa_meat_planner/static/src/components/supply_planner/supply_planner.js',
            'bawa_meat_planner/static/src/components/supply_planner/supply_planner.xml',
            'bawa_meat_planner/static/src/scss/bawa_planner.scss',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
