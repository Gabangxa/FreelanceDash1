// Initialize tooltips and popovers
document.addEventListener('DOMContentLoaded', function() {
    // Initialize Bootstrap tooltips
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });

    // Initialize Bootstrap popovers
    var popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
    popoverTriggerList.map(function (popoverTriggerEl) {
        return new bootstrap.Popover(popoverTriggerEl);
    });
    
    // Initialize dark mode from saved preference
    initThemeMode();
});

// Invoice Items Management
function initInvoiceItems() {
    const invoiceItems = document.getElementById('invoice-items');
    const addItemBtn = document.getElementById('add-item');

    if (invoiceItems && addItemBtn) {
        // Add new item
        addItemBtn.addEventListener('click', function() {
            const items = invoiceItems.getElementsByClassName('invoice-item');
            if (items.length === 0) {
                console.error('No template item found');
                return;
            }

            const newItem = items[0].cloneNode(true);
            const newIndex = items.length;

            // Update form field names for the new index
            newItem.querySelectorAll('input, textarea').forEach(input => {
                const oldName = input.getAttribute('name');
                if (oldName) {
                    // Extract the field name from the current format "items-0-fieldname"
                    const fieldName = oldName.split('-')[2];
                    // Create new name with updated index
                    const newName = `items-${newIndex}-${fieldName}`;
                    input.setAttribute('name', newName);
                    input.setAttribute('id', newName);
                }
            });

            clearInputs(newItem);
            attachItemListeners(newItem);
            invoiceItems.appendChild(newItem);
            updateTotalAmount();
        });

        // Initialize existing items
        document.querySelectorAll('.invoice-item').forEach(item => {
            attachItemListeners(item);
        });
    }
}

function attachItemListeners(item) {
    if (!item) return;

    // Remove item
    const removeBtn = item.querySelector('.remove-item');
    if (removeBtn) {
        removeBtn.addEventListener('click', function() {
            const items = document.querySelectorAll('.invoice-item');
            if (items.length > 1) {
                item.remove();
                // Reindex remaining items
                reindexItems();
                updateTotalAmount();
            }
        });
    }

    // Calculate amount
    const quantityInput = item.querySelector('.quantity');
    const rateInput = item.querySelector('.rate');
    const amountInput = item.querySelector('.amount');

    if (quantityInput && rateInput && amountInput) {
        [quantityInput, rateInput].forEach(input => {
            input.addEventListener('input', function() {
                const quantity = parseFloat(quantityInput.value) || 0;
                const rate = parseFloat(rateInput.value) || 0;
                const amount = quantity * rate;
                amountInput.value = amount.toFixed(2);
                updateTotalAmount();
            });
        });
    }
}

function clearInputs(item) {
    if (!item) return;
    item.querySelectorAll('input, textarea').forEach(input => {
        if (input.type !== 'hidden') {
            input.value = '';
        }
    });
}

function reindexItems() {
    const items = document.querySelectorAll('.invoice-item');
    items.forEach((item, index) => {
        item.querySelectorAll('input, textarea').forEach(input => {
            const oldName = input.getAttribute('name');
            if (oldName) {
                const fieldName = oldName.split('-')[2];
                const newName = `items-${index}-${fieldName}`;
                input.setAttribute('name', newName);
                input.setAttribute('id', newName);
            }
        });
    });
}

function updateTotalAmount() {
    try {
        const amounts = document.querySelectorAll('.amount');
        if (!amounts.length) return;

        const total = Array.from(amounts)
            .map(input => parseFloat(input.value) || 0)
            .reduce((sum, current) => sum + current, 0);

        const totalElement = document.getElementById('preview-total');
        if (totalElement) {
            const currencySelect = document.getElementById('currency');
            const currencySymbol = getCurrencySymbol(currencySelect.value);
            totalElement.textContent = `${currencySymbol}${total.toFixed(2)}`;
        }
    } catch (error) {
        console.error('Error updating total amount:', error);
    }
}

function getCurrencySymbol(currencyCode) {
    const symbols = {
        'USD': '$',
        'EUR': '€',
        'GBP': '£',
        'JPY': '¥',
        'CAD': 'CAD$',
        'AUD': 'A$',
        'ZAR': 'R',
        'NGN': '₦',
        'KES': 'KSh',
        'GHS': '₵',
        'BRL': 'R$',
        'MXN': 'Mex$',
        'SGD': 'S$',
        'AED': 'د.إ'
    };
    return symbols[currencyCode] || currencyCode;
}

// Time Tracking
let timerInterval;
let startTime;

function startTimer() {
    const timerDisplay = document.getElementById('timer-display');
    const startBtn = document.getElementById('start-timer');
    const stopBtn = document.getElementById('stop-timer');

    if (timerDisplay && startBtn && stopBtn) {
        startTime = Date.now();
        timerInterval = setInterval(() => {
            const elapsedTime = Math.floor((Date.now() - startTime) / 1000);
            const hours = Math.floor(elapsedTime / 3600);
            const minutes = Math.floor((elapsedTime % 3600) / 60);
            const seconds = elapsedTime % 60;
            timerDisplay.textContent =
                `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
        }, 1000);

        startBtn.style.display = 'none';
        stopBtn.style.display = 'inline-block';
    }
}

function stopTimer() {
    if (timerInterval) {
        clearInterval(timerInterval);
        const duration = Math.floor((Date.now() - startTime) / 60000); // Duration in minutes

        // Update hidden input for form submission
        const durationInput = document.querySelector('input[name="duration"]');
        if (durationInput) {
            durationInput.value = duration;
        }

        document.getElementById('start-timer').style.display = 'inline-block';
        document.getElementById('stop-timer').style.display = 'none';
    }
}

// Flash Messages
function dismissFlash(element) {
    element.closest('.alert').remove();
}

// Theme Mode Toggle (Dark/Light mode)
function initThemeMode() {
    // Check for saved theme preference or use system preference
    const savedTheme = localStorage.getItem('theme');
    const themeToggle = document.getElementById('theme-toggle');
    const prefersDarkMode = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const darkIcon = document.getElementById('dark-icon');
    const lightIcon = document.getElementById('light-icon');
    const themeText = document.getElementById('theme-text');
    
    // Apply the theme based on saved preference or system preference
    if (savedTheme === 'dark' || (!savedTheme && prefersDarkMode)) {
        document.documentElement.setAttribute('data-bs-theme', 'dark');
        if (themeToggle) {
            themeToggle.checked = true;
        }
        updateThemeIcons('dark');
    } else {
        document.documentElement.setAttribute('data-bs-theme', 'light');
        if (themeToggle) {
            themeToggle.checked = false;
        }
        updateThemeIcons('light');
    }
    
    // Attach event listener to theme toggle switch
    if (themeToggle) {
        themeToggle.addEventListener('change', toggleTheme);
    }
}

function toggleTheme(event) {
    const theme = event.target.checked ? 'dark' : 'light';
    document.documentElement.setAttribute('data-bs-theme', theme);
    localStorage.setItem('theme', theme);
    updateThemeIcons(theme);
}

function updateThemeIcons(theme) {
    const darkIcon = document.getElementById('dark-icon');
    const lightIcon = document.getElementById('light-icon');
    const themeText = document.getElementById('theme-text');
    
    if (theme === 'dark') {
        if (darkIcon) darkIcon.style.display = 'inline-block';
        if (lightIcon) lightIcon.style.display = 'none';
        if (themeText) themeText.textContent = 'Dark Mode';
    } else {
        if (darkIcon) darkIcon.style.display = 'none';
        if (lightIcon) lightIcon.style.display = 'inline-block';
        if (themeText) themeText.textContent = 'Light Mode';
    }
}