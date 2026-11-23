/* LensBazaar OPP — Frontend JS */

// Auto-dismiss alerts after 5 seconds
document.querySelectorAll('.alert').forEach(function(el) {
    setTimeout(function() { el.style.display = 'none'; }, 5000);
});

// Confirm before destructive actions
document.querySelectorAll('[data-confirm]').forEach(function(el) {
    el.addEventListener('click', function(e) {
        if (!confirm(el.dataset.confirm)) {
            e.preventDefault();
        }
    });
});
