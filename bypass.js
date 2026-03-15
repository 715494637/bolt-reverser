 (async () => {
    const uniq = (arr) => Array.from(new Set(arr.filter(Boolean)));
    const manifest = window.__remixManifest;

    const collectCandidates = () => {
      const set = new Set();
      const add = (v) => { if (v) set.add(v); };
      const addAll = (arr) => (arr || []).forEach(add);

      if (manifest?.entry) {
        add(manifest.entry.module);
        addAll(manifest.entry.imports);
      }
      if (manifest?.routes) {
        Object.values(manifest.routes).forEach(r => {
          add(r.module);
          addAll(r.imports);
        });
      }
      document.querySelectorAll('link[rel="modulepreload"]').forEach(l => add(l.getAttribute('href') || l.href));
      return uniq([...set]).filter(u => /\/assets\/.*\.js$/.test(u));
    };

    const findStoreModule = async () => {
      const candidates = collectCandidates();
      for (const url of candidates) {
        try {
          const mod = await import(url);
          const atom = mod?.u?.isPaidUser;
          if (atom && typeof atom.get === 'function' && typeof atom.set === 'function') {
            return { mod, atom, url };
          }
        } catch (_) {}
      }
      return null;
    };

    const hit = await findStoreModule();
    if (!hit) {
      console.warn('isPaidUser atom not found. Reload and retry.');
      return;
    }

    const { atom, url } = hit;

    if (!atom.__origSet) atom.__origSet = atom.set.bind(atom);
    if (!atom.__origGet) atom.__origGet = atom.get.bind(atom);

    atom.__origSet(true);

    if (!atom.__forcePaidSub) {
      atom.__forcePaidSub = atom.subscribe((v) => {
        if (v !== true) atom.__origSet(true);
      });
    }

    // 方便撤销
    window.__unforcePaid = () => {
      try { atom.__forcePaidSub?.(); } catch {}
      delete atom.__forcePaidSub;
      atom.__origSet(false);
    };

    window.__forcePaid = () => atom.__origSet(true);

    console.log('isPaidUser forced true via', url);
  })();