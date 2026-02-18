from django.db import models
from django.utils import timezone
from django.urls import reverse
from django.core.validators import URLValidator
from django.utils.text import slugify
from django.conf import settings


class SEOMixin(models.Model):
    """
    Abstract mixin for SEO fields
    """
    meta_title = models.CharField(
        max_length=200,
        blank=True,
        help_text='SEO title (if blank, will use page title)'
    )
    meta_description = models.TextField(
        max_length=320,
        blank=True,
        help_text='SEO meta description'
    )
    meta_keywords = models.CharField(
        max_length=255,
        blank=True,
        help_text='Comma-separated keywords'
    )
    og_title = models.CharField(
        max_length=200,
        blank=True,
        help_text='Open Graph title (social sharing)'
    )
    og_description = models.TextField(
        max_length=320,
        blank=True,
        help_text='Open Graph description'
    )
    og_image = models.ImageField(
        upload_to='seo/',
        blank=True,
        null=True,
        help_text='Open Graph image (1200x630 recommended)'
    )
    twitter_card = models.CharField(
        max_length=50,
        choices=[
            ('summary', 'Summary'),
            ('summary_large_image', 'Summary Large Image'),
            ('app', 'App'),
            ('player', 'Player'),
        ],
        default='summary_large_image'
    )
    canonical_url = models.URLField(
        blank=True,
        help_text='Custom canonical URL (leave blank to auto-generate)'
    )
    robots = models.CharField(
        max_length=100,
        default='index, follow',
        help_text='Robots meta directive'
    )
    
    class Meta:
        abstract = True


class Testimonial(models.Model):
    """
    Customer testimonials for home page
    """
    name = models.CharField(max_length=100)
    title = models.CharField(max_length=100)
    company = models.CharField(max_length=100)
    avatar = models.ImageField(upload_to='testimonials/', blank=True, null=True)
    content = models.TextField()
    rating = models.PositiveSmallIntegerField(
        choices=[(i, str(i)) for i in range(1, 6)],
        default=5
    )
    featured = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['order', '-created_at']
    
    def __str__(self):
        return f"{self.name} - {self.company}"


class FAQ(models.Model):
    """
    Frequently Asked Questions
    """
    question = models.CharField(max_length=200)
    answer = models.TextField()
    category = models.CharField(
        max_length=50,
        choices=[
            ('general', 'General'),
            ('pricing', 'Pricing & Billing'),
            ('technical', 'Technical'),
            ('support', 'Support'),
        ],
        default='general'
    )
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['category', 'order']
        verbose_name = 'FAQ'
        verbose_name_plural = 'FAQs'
    
    def __str__(self):
        return self.question


class Feature(models.Model):
    """
    Platform features for home page
    """
    title = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    short_description = models.CharField(max_length=200)
    full_description = models.TextField()
    icon = models.CharField(
        max_length=50,
        help_text='Font Awesome icon class (e.g., "fas fa-chart-line")'
    )
    image = models.ImageField(upload_to='features/', blank=True, null=True)
    benefits = models.JSONField(
        default=list,
        help_text='List of benefit points'
    )
    use_cases = models.JSONField(
        default=list,
        help_text='List of use cases'
    )
    is_featured = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['order']
    
    def __str__(self):
        return self.title
    
    def get_absolute_url(self):
        return reverse('core:feature_detail', args=[self.slug])


class Statistic(models.Model):
    """
    Company statistics for home page
    """
    label = models.CharField(max_length=100)
    value = models.CharField(max_length=50)  # e.g., "10K+", "99.9%"
    prefix = models.CharField(max_length=10, blank=True)
    suffix = models.CharField(max_length=10, blank=True)
    icon = models.CharField(max_length=50, blank=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['order']
        verbose_name_plural = 'Statistics'
    
    def __str__(self):
        return f"{self.label}: {self.value}"


class Partner(models.Model):
    """
    Partners and integrations
    """
    name = models.CharField(max_length=100)
    logo = models.ImageField(upload_to='partners/')
    website = models.URLField(blank=True)
    description = models.TextField(blank=True)
    is_featured = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['order']
    
    def __str__(self):
        return self.name


class BlogPost(SEOMixin, models.Model):
    """
    Blog posts for content marketing
    """
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    featured_image = models.ImageField(upload_to='blog/')
    excerpt = models.TextField(max_length=300)
    content = models.TextField()
    category = models.CharField(
        max_length=50,
        choices=[
            ('news', 'News'),
            ('tutorial', 'Tutorial'),
            ('case-study', 'Case Study'),
            ('update', 'Product Update'),
        ]
    )
    tags = models.CharField(max_length=255, blank=True)
    read_time = models.PositiveIntegerField(help_text='Estimated read time in minutes')
    is_featured = models.BooleanField(default=False)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    views = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-published_at']
    
    def __str__(self):
        return self.title
    
    def get_absolute_url(self):
        return reverse('core:blog_detail', args=[self.slug])
    
    def save(self, *args, **kwargs):
        if self.is_published and not self.published_at:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)


class ContactMessage(models.Model):
    """
    Contact form submissions
    """
    name = models.CharField(max_length=100)
    email = models.EmailField()
    company = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    subject = models.CharField(max_length=200)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.name} - {self.subject}"




class SiteSettings(models.Model):
    """
    Global site settings (singleton)
    """
    site_name = models.CharField(max_length=100, default='MetaAnalytics')
    site_tagline = models.CharField(max_length=200, default='Custom Analytics for Everyone')
    logo = models.ImageField(upload_to='site/', blank=True, null=True)
    favicon = models.ImageField(upload_to='site/', blank=True, null=True)
    
    # Contact info
    support_email = models.EmailField(default='support@metaanalytics.com')
    sales_email = models.EmailField(default='sales@metaanalytics.com')
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    
    # Social media
    twitter_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    github_url = models.URLField(blank=True)
    youtube_url = models.URLField(blank=True)
    
    # Analytics
    google_analytics_id = models.CharField(max_length=50, blank=True)
    google_tag_manager_id = models.CharField(max_length=50, blank=True)
    facebook_pixel_id = models.CharField(max_length=50, blank=True)
    
    # SEO
    default_meta_title = models.CharField(max_length=200, default='MetaAnalytics - No-Code Analytics Platform')
    default_meta_description = models.TextField(max_length=320, default='Build your own analytics dashboard without code. Track expenses, sales, inventory, and more with custom categories and automatic visualizations.')
    default_meta_keywords = models.CharField(max_length=255, default='analytics, business intelligence, no-code, data tracking, SME, dashboard')
    
    class Meta:
        verbose_name = 'Site Settings'
        verbose_name_plural = 'Site Settings'
    
    def __str__(self):
        return self.site_name
    
    @classmethod
    def get_settings(cls):
        return cls.objects.first()