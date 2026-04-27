import { useEffect, useState, useCallback } from 'react';

function parseHash() {
  const raw = (typeof window === 'undefined' ? '' : window.location.hash) || '';
  const path = raw.replace(/^#/, '') || '/';
  return path;
}

export function useHashRoute() {
  const [path, setPath] = useState(parseHash());

  useEffect(() => {
    const handler = () => setPath(parseHash());
    window.addEventListener('hashchange', handler);
    return () => window.removeEventListener('hashchange', handler);
  }, []);

  const navigate = useCallback((next, { replace = false } = {}) => {
    const hash = `#${next}`;
    if (replace) {
      const url = `${window.location.pathname}${window.location.search}${hash}`;
      window.history.replaceState(null, '', url);
      setPath(parseHash());
    } else {
      window.location.hash = hash;
    }
  }, []);

  return { path, navigate };
}
