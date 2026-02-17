from django.contrib import admin

from .models import (
    BlogPost,
    Testimonial,
    FAQ,
    ContactMessage,
    Feature,
    NewsletterSubscriber,
    Statistic,
    Partner,
    SiteSettings,
)

admin.site.register(BlogPost)
admin.site.register(Testimonial)
admin.site.register(FAQ)
admin.site.register(ContactMessage)
admin.site.register(Feature)
admin.site.register(NewsletterSubscriber)
admin.site.register(Statistic)
admin.site.register(Partner)
admin.site.register(SiteSettings)