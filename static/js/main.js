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
});

// Invoice Items Management
function initInvoiceItems() {
    const invoiceItems = document.getElementById('invoice-items');
    const addItemBtn = document.getElementById('add-item');

    if (invoiceItems && addItemBtn) {
        // Add new item
        addItemBtn.addEventListener('click', function() {
            const items = invoiceItems.querySelectorAll('.invoice-item');
            const newIndex = items.length;

            if (items.length === 0) {
                console.error('No template item found');
                return;
            }

            const newItem = items[0].cloneNode(true);
            clearInputs(newItem);

            // Update input names for the new index
            newItem.querySelectorAll('input').forEach(input => {
                const nameParts = input.name.split('-');
                if (nameParts.length >= 3) {
                    input.name = `items-${newIndex}-${nameParts[2]}`;
                    input.id = `items-${newIndex}-${nameParts[2]}`;
                }
            });

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
    item.querySelectorAll('input').forEach(input => {
        input.value = '';
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
            totalElement.textContent = `$${total.toFixed(2)}`;
        }
    } catch (error) {
        console.error('Error updating total amount:', error);
    }
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