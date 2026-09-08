// Shared navigation behavior for the home page and optimizer.
(() => {
    const hamburger = document.getElementById('navHamburger');
    const mobileMenu = document.getElementById('navMobileMenu');
    const mobileViewport = window.matchMedia('(max-width: 768px)');

    function setMenuOpen(open) {
        hamburger.classList.toggle('open', open);
        mobileMenu.classList.toggle('open', open);
        hamburger.setAttribute('aria-expanded', String(open));
    }

    hamburger.addEventListener('click', () => {
        setMenuOpen(hamburger.getAttribute('aria-expanded') !== 'true');
    });

    mobileMenu.querySelectorAll('a').forEach(link => {
        link.addEventListener('click', () => setMenuOpen(false));
    });

    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && hamburger.getAttribute('aria-expanded') === 'true') {
            setMenuOpen(false);
            hamburger.focus();
        }
    });

    document.addEventListener('click', event => {
        if (!event.target.closest('#navbar')) setMenuOpen(false);
    });

    mobileViewport.addEventListener('change', () => setMenuOpen(false));
})();
