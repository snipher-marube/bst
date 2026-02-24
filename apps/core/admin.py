from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.db import models as django_models
from .models import (
    Testimonial, FAQ, Feature, Statistic, Partner, 
    BlogPost, ContactMessage, SiteSettings
)


class BaseModelAdmin(admin.ModelAdmin):
    """Base admin class with common configurations"""
    class Meta:
        abstract = True
    
    def get_readonly_fields(self, request, obj=None):
        """Add created_at and updated_at as readonly fields if they exist"""
        readonly_fields = list(super().get_readonly_fields(request, obj))
        time_fields = ['created_at', 'updated_at']
        for field in time_fields:
            if hasattr(self.model, field):
                readonly_fields.append(field)
        return readonly_fields


@admin.register(Testimonial)
class TestimonialAdmin(BaseModelAdmin):
    list_display = ['name', 'company', 'rating_stars', 'featured', 'order', 'is_active', 'created_at']
    list_filter = ['rating', 'featured', 'is_active', 'created_at']
    list_editable = ['featured', 'order', 'is_active']
    search_fields = ['name', 'company', 'content']
    list_per_page = 20
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'title', 'company', 'avatar', 'content')
        }),
        ('Rating & Display', {
            'fields': ('rating', 'featured', 'order', 'is_active')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def rating_stars(self, obj):
        """Display rating as stars"""
        stars = '★' * obj.rating + '☆' * (5 - obj.rating)
        return format_html('<span style="color: #fbbf24;">{}</span>', stars)
    rating_stars.short_description = 'Rating'
    rating_stars.admin_order_field = 'rating'


@admin.register(FAQ)
class FAQAdmin(BaseModelAdmin):
    list_display = ['question', 'category', 'order', 'is_active', 'updated_at']
    list_filter = ['category', 'is_active', 'created_at']
    list_editable = ['category', 'order', 'is_active']
    search_fields = ['question', 'answer']
    list_per_page = 30
    
    fieldsets = (
        ('Question & Answer', {
            'fields': ('question', 'answer', 'category')
        }),
        ('Display Settings', {
            'fields': ('order', 'is_active')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


class FeatureBenefitInline(admin.TabularInline):
    """Inline admin for Feature benefits (if you want to make benefits model-based)"""
    model = Feature.benefits  # This would need adjustment if benefits become a separate model
    extra = 1
    can_delete = True


@admin.register(Feature)
class FeatureAdmin(BaseModelAdmin):
    list_display = ['title', 'icon_preview', 'is_featured', 'order', 'is_active', 'updated_at']
    list_filter = ['is_featured', 'is_active', 'created_at']
    list_editable = ['is_featured', 'order', 'is_active']
    search_fields = ['title', 'short_description', 'full_description']
    prepopulated_fields = {'slug': ('title',)}
    list_per_page = 20
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('title', 'slug', 'icon', 'image')
        }),
        ('Description', {
            'fields': ('short_description', 'full_description')
        }),
        ('Details', {
            'fields': ('benefits', 'use_cases')
        }),
        ('Display Settings', {
            'fields': ('is_featured', 'order', 'is_active')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def icon_preview(self, obj):
        """Display icon preview"""
        if obj.icon:
            return format_html('<i class="{}" style="font-size: 1.2rem;"></i>', obj.icon)
        return '-'
    icon_preview.short_description = 'Icon'
    
    def get_form(self, request, obj=None, **kwargs):
        """Make benefits and use cases fields more user-friendly"""
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['benefits'].widget = admin.widgets.AdminTextareaWidget(
            attrs={'rows': 3, 'class': 'vLargeTextField', 'placeholder': 'Enter each benefit on a new line'}
        )
        form.base_fields['use_cases'].widget = admin.widgets.AdminTextareaWidget(
            attrs={'rows': 3, 'class': 'vLargeTextField', 'placeholder': 'Enter each use case on a new line'}
        )
        return form


@admin.register(Statistic)
class StatisticAdmin(BaseModelAdmin):
    list_display = ['label', 'value_display', 'order', 'is_active']
    list_filter = ['is_active']
    list_editable = ['order', 'is_active']
    search_fields = ['label', 'value']
    list_per_page = 20
    
    fieldsets = (
        ('Statistic Details', {
            'fields': ('label', 'value', 'prefix', 'suffix', 'icon')
        }),
        ('Display Settings', {
            'fields': ('order', 'is_active')
        }),
    )
    
    def value_display(self, obj):
        """Display value with prefix/suffix"""
        parts = []
        if obj.prefix:
            parts.append(obj.prefix)
        parts.append(obj.value)
        if obj.suffix:
            parts.append(obj.suffix)
        return ' '.join(parts)
    value_display.short_description = 'Display Value'


@admin.register(Partner)
class PartnerAdmin(BaseModelAdmin):
    list_display = ['name', 'logo_preview', 'is_featured', 'order', 'is_active']
    list_filter = ['is_featured', 'is_active']
    list_editable = ['is_featured', 'order', 'is_active']
    search_fields = ['name', 'description']
    list_per_page = 20
    
    fieldsets = (
        ('Partner Information', {
            'fields': ('name', 'logo', 'website', 'description')
        }),
        ('Display Settings', {
            'fields': ('is_featured', 'order', 'is_active')
        }),
    )
    
    def logo_preview(self, obj):
        """Display logo thumbnail"""
        if obj.logo:
            return format_html(
                '<img src="{}" style="max-height: 50px; max-width: 100px;" />',
                obj.logo.url
            )
        return '-'
    logo_preview.short_description = 'Logo Preview'


@admin.register(BlogPost)
class BlogPostAdmin(BaseModelAdmin):
    list_display = ['title', 'author', 'category', 'read_time', 'is_published', 'is_featured', 'views', 'published_at']
    list_filter = ['category', 'is_published', 'is_featured', 'author', 'created_at']
    list_editable = ['is_published', 'is_featured']
    search_fields = ['title', 'excerpt', 'content', 'tags']
    prepopulated_fields = {'slug': ('title',)}
    date_hierarchy = 'published_at'
    list_per_page = 20
    raw_id_fields = ['author']
    
    fieldsets = (
        ('Content', {
            'fields': ('title', 'slug', 'author', 'featured_image', 'excerpt', 'content')
        }),
        ('Metadata', {
            'fields': ('category', 'tags', 'read_time')
        }),
        ('Publication', {
            'fields': ('is_published', 'is_featured', 'published_at')
        }),
        ('SEO', {
            'fields': (
                'meta_title', 'meta_description', 'meta_keywords',
                'og_title', 'og_description', 'og_image',
                'twitter_card', 'canonical_url', 'robots'
            ),
            'classes': ('collapse',)
        }),
        ('Statistics', {
            'fields': ('views',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    actions = ['publish_posts', 'unpublish_posts', 'mark_as_featured']
    
    def publish_posts(self, request, queryset):
        """Bulk publish posts"""
        updated = queryset.update(is_published=True)
        self.message_user(request, f'{updated} posts were successfully published.')
    publish_posts.short_description = 'Publish selected posts'
    
    def unpublish_posts(self, request, queryset):
        """Bulk unpublish posts"""
        updated = queryset.update(is_published=False)
        self.message_user(request, f'{updated} posts were successfully unpublished.')
    unpublish_posts.short_description = 'Unpublish selected posts'
    
    def mark_as_featured(self, request, queryset):
        """Bulk mark as featured"""
        updated = queryset.update(is_featured=True)
        self.message_user(request, f'{updated} posts were marked as featured.')
    mark_as_featured.short_description = 'Mark as featured'


@admin.register(ContactMessage)
class ContactMessageAdmin(BaseModelAdmin):
    list_display = ['name', 'email', 'subject_preview', 'company', 'is_read', 'created_at']
    list_filter = ['is_read', 'created_at']
    list_editable = ['is_read']
    search_fields = ['name', 'email', 'company', 'subject', 'message']
    date_hierarchy = 'created_at'
    list_per_page = 30
    readonly_fields = ['name', 'email', 'company', 'phone', 'subject', 'message', 'created_at']
    
    fieldsets = (
        ('Contact Information', {
            'fields': ('name', 'email', 'company', 'phone')
        }),
        ('Message', {
            'fields': ('subject', 'message')
        }),
        ('Status', {
            'fields': ('is_read', 'created_at')
        }),
    )
    
    def subject_preview(self, obj):
        """Show truncated subject"""
        return obj.subject[:50] + '...' if len(obj.subject) > 50 else obj.subject
    subject_preview.short_description = 'Subject'
    
    actions = ['mark_as_read', 'mark_as_unread']
    
    def mark_as_read(self, request, queryset):
        """Bulk mark as read"""
        updated = queryset.update(is_read=True)
        self.message_user(request, f'{updated} messages were marked as read.')
    mark_as_read.short_description = 'Mark as read'
    
    def mark_as_unread(self, request, queryset):
        """Bulk mark as unread"""
        updated = queryset.update(is_read=False)
        self.message_user(request, f'{updated} messages were marked as unread.')
    mark_as_unread.short_description = 'Mark as unread'
    
    def has_add_permission(self, request):
        """Prevent adding messages manually"""
        return False


@admin.register(SiteSettings)
class SiteSettingsAdmin(BaseModelAdmin):
    """Singleton admin for site settings"""
    
    def has_add_permission(self, request):
        """Prevent adding multiple instances"""
        return not SiteSettings.objects.exists()
    
    def has_delete_permission(self, request, obj=None):
        """Prevent deletion of site settings"""
        return False
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('site_name', 'site_tagline', 'logo', 'favicon')
        }),
        ('Contact Information', {
            'fields': ('support_email', 'sales_email', 'phone', 'address')
        }),
        ('Social Media', {
            'fields': ('twitter_url', 'linkedin_url', 'github_url', 'youtube_url'),
            'classes': ('collapse',)
        }),
        ('Analytics', {
            'fields': ('google_analytics_id', 'google_tag_manager_id', 'facebook_pixel_id'),
            'classes': ('collapse',)
        }),
        ('Default SEO', {
            'fields': ('default_meta_title', 'default_meta_description', 'default_meta_keywords'),
            'classes': ('collapse',)
        }),
    )
    
    def get_readonly_fields(self, request, obj=None):
        """No readonly fields except for existing instance"""
        if obj:
            return []  # Allow editing all fields
        return super().get_readonly_fields(request, obj)