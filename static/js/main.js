// Main JavaScript file for MetaAnalytics

document.addEventListener('DOMContentLoaded', function() {
    initializeMobileMenu();
    initializeDropdowns();
    initializeTooltips();
    initializeLazyLoading();
    initializeFormValidation();
    initializeScrollSpy();
    initializeBackToTop();
});

// Mobile menu functionality
function initializeMobileMenu() {
    const menuButton = document.querySelector('.mobile-menu-button');
    const closeButton = document.querySelector('.mobile-menu-close');
    const mobileMenu = document.querySelector('.mobile-menu');
    
    if (!menuButton || !mobileMenu) return;
    
    function openMenu() {
        mobileMenu.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
        menuButton.setAttribute('aria-expanded', 'true');
        
        // Trigger reflow for animation
        void mobileMenu.offsetWidth;
        
        const menuPanel = mobileMenu.querySelector('.fixed.inset-y-0.right-0');
        if (menuPanel) {
            menuPanel.classList.add('translate-x-0');
            menuPanel.classList.remove('translate-x-full');
        }
    }
    
    function closeMenu() {
        const menuPanel = mobileMenu.querySelector('.fixed.inset-y-0.right-0');
        if (menuPanel) {
            menuPanel.classList.remove('translate-x-0');
            menuPanel.classList.add('translate-x-full');
        }
        
        setTimeout(() => {
            mobileMenu.classList.add('hidden');
            document.body.style.overflow = '';
            menuButton.setAttribute('aria-expanded', 'false');
        }, 300);
    }
    
    menuButton.addEventListener('click', openMenu);
    
    if (closeButton) {
        closeButton.addEventListener('click', closeMenu);
    }
    
    // Close on overlay click
    mobileMenu.addEventListener('click', function(e) {
        if (e.target === mobileMenu || e.target.classList.contains('bg-gray-600')) {
            closeMenu();
        }
    });
    
    // Close on escape key
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && !mobileMenu.classList.contains('hidden')) {
            closeMenu();
        }
    });
}

// Dropdown menus
function initializeDropdowns() {
    document.querySelectorAll('[data-dropdown]').forEach(dropdown => {
        const button = dropdown.querySelector('.dropdown-button');
        const menu = dropdown.querySelector('.dropdown-menu');
        
        if (!button || !menu) return;
        
        button.addEventListener('click', (e) => {
            e.stopPropagation();
            const isExpanded = button.getAttribute('aria-expanded') === 'true';
            
            // Close all other dropdowns
            document.querySelectorAll('[data-dropdown] .dropdown-menu').forEach(m => {
                if (m !== menu) {
                    m.classList.add('hidden');
                    m.previousElementSibling?.setAttribute('aria-expanded', 'false');
                }
            });
            
            menu.classList.toggle('hidden');
            button.setAttribute('aria-expanded', (!isExpanded).toString());
        });
        
        // Close on click outside
        document.addEventListener('click', (e) => {
            if (!dropdown.contains(e.target)) {
                menu.classList.add('hidden');
                button.setAttribute('aria-expanded', 'false');
            }
        });
    });
}

// Tooltips
function initializeTooltips() {
    document.querySelectorAll('[data-tooltip]').forEach(element => {
        const tooltipText = element.getAttribute('data-tooltip');
        
        element.addEventListener('mouseenter', (e) => {
            const tooltip = document.createElement('div');
            tooltip.className = 'absolute z-50 px-2 py-1 text-sm text-white bg-gray-900 rounded shadow-lg whitespace-nowrap';
            tooltip.textContent = tooltipText;
            tooltip.style.top = e.target.offsetTop - 30 + 'px';
            tooltip.style.left = e.target.offsetLeft + 'px';
            tooltip.id = 'tooltip-' + Math.random().toString(36).substr(2, 9);
            
            element.style.position = 'relative';
            element.appendChild(tooltip);
        });
        
        element.addEventListener('mouseleave', () => {
            const tooltip = element.querySelector('div[id^="tooltip-"]');
            if (tooltip) {
                tooltip.remove();
            }
        });
    });
}

// Lazy loading images
function initializeLazyLoading() {
    if ('IntersectionObserver' in window) {
        const imageObserver = new IntersectionObserver((entries, observer) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const img = entry.target;
                    const src = img.getAttribute('data-src');
                    
                    if (src) {
                        img.src = src;
                        img.classList.add('loaded');
                    }
                    
                    imageObserver.unobserve(img);
                }
            });
        });
        
        document.querySelectorAll('img[data-src]').forEach(img => {
            imageObserver.observe(img);
        });
    } else {
        // Fallback for older browsers
        document.querySelectorAll('img[data-src]').forEach(img => {
            img.src = img.getAttribute('data-src');
        });
    }
}

// Form validation
function initializeFormValidation() {
    document.querySelectorAll('form[data-validate]').forEach(form => {
        form.addEventListener('submit', (e) => {
            let isValid = true;
            
            form.querySelectorAll('[required]').forEach(field => {
                if (!field.value.trim()) {
                    isValid = false;
                    showFieldError(field, 'This field is required');
                } else {
                    clearFieldError(field);
                }
            });
            
            form.querySelectorAll('[type="email"]').forEach(field => {
                if (field.value && !isValidEmail(field.value)) {
                    isValid = false;
                    showFieldError(field, 'Please enter a valid email address');
                }
            });
            
            if (!isValid) {
                e.preventDefault();
            }
        });
    });
}

function isValidEmail(email) {
    const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    return re.test(email);
}

function showFieldError(field, message) {
    clearFieldError(field);
    
    field.classList.add('border-red-500');
    
    const error = document.createElement('p');
    error.className = 'text-sm text-red-600 mt-1 error-message';
    error.textContent = message;
    
    field.parentNode.appendChild(error);
}

function clearFieldError(field) {
    field.classList.remove('border-red-500');
    
    const error = field.parentNode.querySelector('.error-message');
    if (error) {
        error.remove();
    }
}

// Scroll spy for navigation
function initializeScrollSpy() {
    const sections = document.querySelectorAll('section[id]');
    const navLinks = document.querySelectorAll('.nav-link');
    
    if (!sections.length || !navLinks.length) return;
    
    window.addEventListener('scroll', () => {
        let current = '';
        const scrollPosition = window.scrollY + 100;
        
        sections.forEach(section => {
            const sectionTop = section.offsetTop;
            const sectionBottom = sectionTop + section.offsetHeight;
            
            if (scrollPosition >= sectionTop && scrollPosition < sectionBottom) {
                current = section.getAttribute('id');
            }
        });
        
        navLinks.forEach(link => {
            link.classList.remove('text-blue-600');
            const href = link.getAttribute('href');
            if (href && href.includes(current)) {
                link.classList.add('text-blue-600');
            }
        });
    });
}

// Back to top button
function initializeBackToTop() {
    const button = document.createElement('button');
    button.className = 'fixed bottom-8 right-8 bg-blue-600 text-white w-12 h-12 rounded-full shadow-lg hover:bg-blue-700 transition-all duration-200 hidden items-center justify-center z-50';
    button.innerHTML = '<i class="fas fa-arrow-up"></i>';
    button.setAttribute('aria-label', 'Back to top');
    
    document.body.appendChild(button);
    
    window.addEventListener('scroll', () => {
        if (window.scrollY > 300) {
            button.classList.remove('hidden');
            button.classList.add('flex');
        } else {
            button.classList.add('hidden');
            button.classList.remove('flex');
        }
    });
    
    button.addEventListener('click', () => {
        window.scrollTo({
            top: 0,
            behavior: 'smooth'
        });
    });
}

// Utility function for AJAX requests
async function fetchAPI(url, options = {}) {
    const defaultOptions = {
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCookie('csrftoken')
        },
        credentials: 'same-origin'
    };
    
    const mergedOptions = { ...defaultOptions, ...options };
    
    try {
        const response = await fetch(url, mergedOptions);
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        return await response.json();
    } catch (error) {
        console.error('API request failed:', error);
        throw error;
    }
}

// Get CSRF token from cookies
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

// Debounce function for performance
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Throttle function for performance
function throttle(func, limit) {
    let inThrottle;
    return function(...args) {
        if (!inThrottle) {
            func.apply(this, args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

// Export utilities for use in other scripts
window.MetaAnalytics = {
    fetchAPI,
    getCookie,
    debounce,
    throttle,
    showToast: (message, type = 'info') => {
        const toast = document.createElement('div');
        toast.className = `toast bg-${type === 'error' ? 'red' : 'blue'}-600 text-white px-6 py-3 rounded-lg shadow-lg`;
        toast.textContent = message;
        
        document.body.appendChild(toast);
        
        setTimeout(() => {
            toast.remove();
        }, 3000);
    }
};