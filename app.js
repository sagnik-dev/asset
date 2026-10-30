const state = {
  files: [],
  profile: null,
  githubDiscovery: null,
  repositories: [],
  references: [],
  analysis: null,
  config: null
};

const els = {
  input: document.querySelector('#source-folder'),
  dropzone: document.querySelector('#dropzone'),
  fileSummary: document.querySelector('#file-summary'),
  fileCount: document.querySelector('#file-count'),
  sourceList: document.querySelector('#source-list'),
  clearFiles: document.querySelector('#clear-files'),
  analyze: document.querySelector('#analyze-button'),
  error: document.querySelector('#error-state'),
  errorMessage: document.querySelector('#error-message'),
  dismissError: document.querySelector('#dismiss-error'),
  empty: document.querySelector('#empty-state'),
  loading: document.querySelector('#loading-state'),
  loadingMessage: document.querySelector('#loading-message'),
  output: document.querySelector('#analysis-output'),
  title: document.querySelector('#results-title'),
  stamp: document.querySelector('.output-stamp'),
  repoList: document.querySelector('#repository-list'),
  compare: document.querySelector('#compare-button'),
  rateLimit: document.querySelector('#rate-limit'),
  ollamaUrl: document.querySelector('#ollama-url'),
  modelName: document.querySelector('#model-name')
};

const ignored = /(^|\\|\/)(node_modules|\.git|dist|build|coverage|\.next|\.cache|venv|\.venv|vendor|target|__pycache__)(\\|\/|$)/i;
const allowed = /\.(js|jsx|ts|tsx|py|java|kt|go|rs|rb|php|cs|c|cpp|h|hpp|html|css|scss|vue|svelte|sql|json|yaml|yml|md|txt|toml|ini|cfg|xml)$/i;

els.input.addEventListener('change', () => setFiles([...els.input.files]));
els.dropzone.addEventListener('dragover', (event) => { event.preventDefault(); els.dropzone.classList.add('dragging'); });
els.dropzone.addEventListener('dragleave', () => els.dropzone.classList.remove('dragging'));
els.dropzone.addEventListener('drop', (event) => { event.preventDefault(); els.dropzone.classList.remove('dragging'); setFiles([...event.dataTransfer.files]); });
els.clearFiles.addEventListener('click', clearProject);
els.dismissError.addEventListener('click', hideError);
els.analyze.addEventListener('click', analyzeProject);
els.compare.addEventListener('click', compareProjects);

async function loadConfig() {
  try {
    state.config = await api('/api/config');
    if (els.ollamaUrl && state.config.ollamaUrl) els.ollamaUrl.value = state.config.ollamaUrl;
    if (els.modelName && state.config.model) els.modelName.value = state.config.model;
  } catch (_) {
    // The backend may not be running yet; the normal API error will explain that.
  }
}
loadConfig();

function setFiles(files) {
  state.files = files.filter((file) => !ignored.test(file.webkitRelativePath || file.name) && allowed.test(file.name));
  state.profile = null;
  state.githubDiscovery = null;
  state.analysis = null;
  state.repositories = [];
  state.references = [];
  renderFiles();
  els.repoList.innerHTML = '';
  els.compare.disabled = true;
}

function clearProject() {
  state.files = [];
  state.profile = null;
  state.githubDiscovery = null;
  state.analysis = null;
  state.repositories = [];
  state.references = [];
  els.input.value = '';
  renderFiles();
  els.output.hidden = true;
  els.output.innerHTML = '';
  els.empty.hidden = false;
  els.title.innerHTML = 'Your analysis<br /><em>will appear here.</em>';
  els.stamp.textContent = 'NOT RUN';
  els.repoList.innerHTML = '';
  els.compare.disabled = true;
}

function renderFiles() {
  const count = state.files.length;
  els.fileSummary.hidden = !count;
  els.fileCount.textContent = `${count} source file${count === 1 ? '' : 's'}`;
  els.analyze.disabled = !count;
  els.sourceList.innerHTML = state.files.slice(0, 8).map((file) => {
    const path = file.webkitRelativePath || file.name;
    return `<div class="source-file" title="${escapeHtml(path)}">${escapeHtml(path)}</div>`;
  }).join('') + (count > 8 ? `<div class="source-file">+ ${count - 8} more files</div>` : '');
}

async function projectPayload() {
  const paths = state.files.map((file) => file.webkitRelativePath || file.name);
  const roots = paths.map((path) => path.replace(/\\/g, '/').split('/')[0]).filter(Boolean);
  const commonRoot = roots.length && roots.every((root) => root === roots[0]) ? `${roots[0]}/` : '';
  return Promise.all(state.files.map(async (file) => {
    const fullPath = file.webkitRelativePath || file.name;
    return { path: fullPath.startsWith(commonRoot) ? fullPath.slice(commonRoot.length) : fullPath, content: await file.text() };
  }));
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`http://127.0.0.1:8000${path}`, options);
  } catch (error) {
    throw new Error('ASSET backend is not reachable. Start server.py and try again.');
  }
  const text = await response.text();
  let data;
  try { data = JSON.parse(text); } catch (_) { throw new Error(`ASSET returned an invalid response (${response.status}).`); }
  if (!response.ok) throw new Error(data.error || `ASSET returned HTTP ${response.status}.`);
  return data;
}

async function analyzeProject() {
  setLoading(true, 'Analyzing the uploaded project first, then generating GitHub discovery queries…');
  hideError();
  try {
    const projectFiles = await projectPayload();
    const result = await api('/api/analyze-project', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        projectFiles,
        ollamaUrl: els.ollamaUrl.value.trim(),
        model: els.modelName.value.trim()
      })
    });

    state.profile = result;
    state.githubDiscovery = result.githubDiscovery || null;
    state.repositories = state.githubDiscovery?.repositories || [];

    renderProfile(result);
    renderGithubDiscovery(state.githubDiscovery);
  } catch (error) {
    showError(error.message);
  } finally {
    setLoading(false);
  }
}

async function searchGithub() {
  const query = els.githubQuery.value.trim() || `${els.githubFramework.value.trim()} software project`;
  els.searchGithub.disabled = true;
  hideError();
  try {
    const data = await api('/api/github/search', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, language: els.githubLanguage.value.trim(), framework: els.githubFramework.value.trim(), limit: 10 })
    });
    state.repositories = data.repositories || [];
    renderRepositories(state.repositories);
    els.rateLimit.textContent = data.rateLimit ? `${data.rateLimit} GitHub requests remaining${data.authenticated ? ' · authenticated' : ''}` : 'GitHub rate limit unavailable';
  } catch (error) { showError(error.message); }
  finally { els.searchGithub.disabled = false; }
}


function renderGithubDiscovery(discoveryResult) {
  const discovery = discoveryResult?.discovery || {};
  const queries = discoveryResult?.queriesRun || discovery?.github_queries || [];
  const repositories = discoveryResult?.repositories || [];

  let section = document.querySelector('#github-discovery-output');
  if (!section) {
    section = document.createElement('section');
    section.id = 'github-discovery-output';
    section.className = 'output-section github-discovery-output';
    els.output.appendChild(section);
  }

  const technologies = [
    ...(discovery.languages || []),
    ...(discovery.frameworks || []),
    ...(discovery.libraries || []),
    ...(discovery.technologies || [])
  ].filter(Boolean);

  section.innerHTML = `
    <div class="github-discovery-header">
      <div>
        <p class="eyebrow">03 / GITHUB DISCOVERY</p>
        <h3>Analysis-driven public references</h3>
      </div>
      <span class="execution-status ready">${repositories.length} CANDIDATES</span>
    </div>
    <p class="strategy-summary">${escapeHtml(discovery.project_summary || 'GitHub discovery was generated from the analyzed project.')}</p>
    ${technologies.length ? `<div class="discovery-tech">${technologies.slice(0, 20).map((item) => `<span>${escapeHtml(item)}</span>`).join('')}</div>` : ''}
    <div class="output-section discovery-query-section">
      <h3>Queries generated from the analysis <span>${queries.length}</span></h3>
      <div class="discovery-query-list">
        ${queries.length
          ? queries.map((query, index) => `<span class="discovery-query"><b>${index + 1}</b>${escapeHtml(query)}</span>`).join('')
          : '<span class="field-hint">No discovery queries were generated.</span>'}
      </div>
    </div>
    <div class="output-section">
      <h3>Candidate repositories <span>${repositories.length}</span></h3>
      <p class="field-hint">Select repositories below, then compare their actual retrieved source with your uploaded project.</p>
      <div class="repository-list" id="automatic-repository-list"></div>
    </div>
  `;

  const list = section.querySelector('#automatic-repository-list');
  if (list) {
    list.innerHTML = repositories.length
      ? repositories.map((repo, index) => `
        <label class="repository-card">
          <input type="checkbox" data-repo-index="${index}" />
          <strong>${escapeHtml(repo.fullName || '')}</strong>
          <span>${escapeHtml(repo.language || 'Unknown')} · ${repo.stars || 0} stars · ${repo.forks || 0} forks · ${escapeHtml(repo.license || 'No license metadata')}</span>
          <p>${escapeHtml(repo.description || 'No repository description.')}</p>
          <a href="${escapeHtml(repo.url || '#')}" target="_blank" rel="noopener noreferrer">View repository</a>
        </label>`).join('')
      : '<p class="field-hint">No public repositories were returned for the generated queries.</p>';

    list.querySelectorAll('input[data-repo-index]').forEach((input) => {
      input.addEventListener('change', syncCompareSelection);
    });
  }

  els.repoList.innerHTML = '';
  els.compare.disabled = !repositories.length;
  els.rateLimit.textContent = `Automatic discovery searched ${queries.length} analysis-generated quer${queries.length === 1 ? 'y' : 'ies'}.`;
}

function syncCompareSelection() {
  els.compare.disabled = document.querySelectorAll('#automatic-repository-list input[data-repo-index]:checked').length === 0;
}

function selectedRepositories() {
  const root = document.querySelector('#automatic-repository-list');
  if (!root) return [];
  return [...root.querySelectorAll('input[data-repo-index]:checked')]
    .map((input) => state.repositories[Number(input.dataset.repoIndex)])
    .filter(Boolean);
}

function renderRepositories(repositories) {
  els.repoList.innerHTML = repositories.length ? repositories.map((repo, index) => `
    <label class="repository-card">
      <input type="checkbox" data-repo-index="${index}" />
      <strong>${escapeHtml(repo.fullName || '')}</strong>
      <span>${escapeHtml(repo.language || 'Unknown')} · ${repo.stars || 0} stars · ${repo.forks || 0} forks · ${escapeHtml(repo.license || 'No license metadata')}</span>
      <p>${escapeHtml(repo.description || 'No repository description.')}</p>
      <a href="${escapeHtml(repo.url || '#')}" target="_blank" rel="noopener noreferrer">View repository</a>
    </label>`).join('') : '<p class="field-hint">No repositories found. Refine the query or framework.</p>';
  els.compare.disabled = !repositories.length;
}

async function compareProjects() {
  const selected = selectedRepositories();
  if (!selected.length) { showError('Select at least one GitHub repository from the discovered references.'); return; }
  if (selected.length > 5) { showError('Select at most 5 repositories for one comparison.'); return; }
  if (!state.files.length) { showError('Upload your project before comparing it.'); return; }
  els.compare.disabled = true;
  setLoading(true, 'Retrieving actual GitHub source and comparing it with the uploaded source…');
  hideError();
  try {
    state.references = [];
    for (const repo of selected) {
      const reference = await api('/api/github/repository', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fullName: repo.fullName, ref: repo.defaultBranch })
      });
      state.references.push(reference);
    }
    const projectFiles = await projectPayload();
    state.analysis = await api('/api/github/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ projectFiles, references: state.references })
    });
    renderComparison(state.analysis);
  } catch (error) { showError(error.message); }
  finally { els.compare.disabled = false; setLoading(false); }
}

function renderProfile(result) {
  const facts = result.inventory || {};
  const profile = result.profile || {};
  els.empty.hidden = true;
  els.output.hidden = false;
  els.title.innerHTML = 'Project profile<br /><em>is ready.</em>';
  els.stamp.textContent = 'ANALYZED';
  els.output.innerHTML = `
    <p class="strategy-summary">The project inventory is computed locally first. Qwen's claims are constrained to the uploaded source; unsupported conclusions are kept as unknowns.</p>
    ${statsHtml([['fileCount', facts.fileCount || 0, 'Source files'], ['dependencies', facts.dependencies?.length || 0, 'Dependencies'], ['symbols', facts.symbols?.length || 0, 'Detected symbols']])}
    ${sectionHtml('Detected project', profile.project || facts)}
    ${sectionHtml('Structure', profile.structure || { sourceDirectories: facts.sourceDirectories, configFiles: facts.configFiles, testFiles: facts.testFiles })}
    ${sectionHtml('Features / risks / unknowns', { features: profile.features || [], risks: profile.risks || [], unknowns: profile.unknowns || [] })}`;
}

function renderComparison(result) {
  const comparison = result.comparison || {};
  const report = comparison.report || {};
  const matches = comparison.matches || [];
  let section = document.querySelector('#github-comparison-output');

  if (!section) {
    section = document.createElement('section');
    section.id = 'github-comparison-output';
    section.className = 'output-section github-comparison-output';
    els.output.appendChild(section);
  }

  const repositories = (comparison.referenceInventories || [])
    .map((item) => item.repository?.fullName || 'Unknown repository');

  section.innerHTML = `
    <div class="github-discovery-header">
      <div>
        <p class="eyebrow">04 / SOURCE COMPARISON</p>
        <h3>Actual source comparison</h3>
      </div>
      <span class="execution-status passed">EVIDENCE READY</span>
    </div>
    <p class="strategy-summary">Retrieved GitHub source is compared with the uploaded source. Deterministic exact and structural matches are the authoritative code-match evidence; Qwen explains that evidence.</p>
    ${statsHtml([
      ['repositories', repositories.length, 'References'],
      ['exactMatches', comparison.exactMatches?.length || 0, 'Exact normalized matches'],
      ['similarMatches', matches.length, 'Code matches']
    ])}
    ${sectionHtml('Reference projects', repositories)}
    ${sectionHtml('Code matches', matches)}
    ${sectionHtml('Evidence-based assessment', report)}
  `;

  els.empty.hidden = true;
  els.output.hidden = false;
  els.title.innerHTML = 'Source comparison<br /><em>is ready.</em>';
  els.stamp.textContent = 'COMPARED';
  section.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function statsHtml(items) {
  return `<div class="stat-grid">${items.map(([, value, label]) => `<div class="stat"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join('')}</div>`;
}

function sectionHtml(title, value) {
  const key = String(title).toLowerCase();
  if (key.includes('features / risks / unknowns')) {
    const obj = value && typeof value === 'object' ? value : {};
    return `
      <section class="output-section">
        <h3>${escapeHtml(title)}</h3>
        <div class="result-columns">
          ${resultListCard('FEATURES', obj.features, 'good')}
          ${resultListCard('RISKS', obj.risks, 'risk')}
          ${resultListCard('UNKNOWNS', obj.unknowns, 'unknown')}
        </div>
      </section>`;
  }
  if (key === 'code matches') {
    const matches = Array.isArray(value) ? value : [];
    return `
      <section class="output-section">
        <h3>${escapeHtml(title)} <span>${matches.length}</span></h3>
        ${matches.length ? `<div class="match-list">${matches.map((match, i) => matchCard(match, i)).join('')}</div>`
          : `<div class="empty-result"><strong>No verified code matches found</strong><p>ASSET did not find an exact normalized or structural source match in the retrieved repositories.</p></div>`}
      </section>`;
  }
  if (key === 'evidence-based assessment') {
    return assessmentHtml(value);
  }
  if (key === 'reference projects') {
    const refs = Array.isArray(value) ? value : [];
    return `
      <section class="output-section">
        <h3>${escapeHtml(title)} <span>${refs.length}</span></h3>
        <div class="reference-list">${refs.length ? refs.map(ref => `<div class="reference-item">${escapeHtml(typeof ref === 'string' ? ref : formatResultValue(ref))}</div>`).join('') : '<div class="empty-result">No reference repositories were retrieved.</div>'}</div>
      </section>`;
  }
  return `
    <section class="output-section">
      <h3>${escapeHtml(title)}</h3>
      ${renderObject(value)}
    </section>`;
}

function renderObject(value) {
  if (value === null || value === undefined) return '<div class="empty-result">No information returned.</div>';
  if (Array.isArray(value)) return resultList(value);
  if (typeof value !== 'object') return `<div class="result-card"><p>${escapeHtml(String(value))}</p></div>`;
  const entries = Object.entries(value);
  if (!entries.length) return '<div class="empty-result">No information returned.</div>';
  return `<div class="result-grid">${entries.map(([key, val]) => `
    <article class="result-card">
      <span class="result-label">${escapeHtml(humanizeKey(key))}</span>
      <div class="result-value">${renderValue(val)}</div>
    </article>`).join('')}</div>`;
}

function renderValue(value) {
  if (Array.isArray(value)) return value.length ? resultList(value) : '<span class="muted-result">None reported.</span>';
  if (value && typeof value === 'object') return renderObject(value);
  if (value === null || value === undefined || value === '') return '<span class="muted-result">Not provided.</span>';
  return escapeHtml(String(value));
}

function resultList(items) {
  if (!Array.isArray(items) || !items.length) return '<div class="muted-result">None reported.</div>';
  return `<ul class="result-list">${items.map(item => `<li>${escapeHtml(formatResultValue(item))}</li>`).join('')}</ul>`;
}

function resultListCard(title, items, tone) {
  return `<article class="result-card result-card-${tone}">
    <span class="result-label">${escapeHtml(title)}</span>
    ${resultList(items)}
  </article>`;
}

function matchCard(match, index) {
  const similarity = Number(match.similarity);
  const percent = Number.isFinite(similarity) ? `${(similarity <= 1 ? similarity * 100 : similarity).toFixed(1)}%` : '—';
  return `<article class="match-card">
    <div class="match-header">
      <span class="match-number">MATCH ${String(index + 1).padStart(2, '0')}</span>
      <span class="match-type">${escapeHtml(match.type || 'STRUCTURAL MATCH')}</span>
      <strong>${percent}</strong>
    </div>
    <div class="match-path-grid">
      <div><span>LOCAL SOURCE</span><code>${escapeHtml(match.localFile || 'Unknown')}</code></div>
      <div><span>REFERENCE SOURCE</span><code>${escapeHtml((match.referenceRepository || '') + (match.referenceFile ? '/' + match.referenceFile : ''))}</code></div>
    </div>
    <p>${escapeHtml(match.evidence || 'No evidence description supplied.')}</p>
  </article>`;
}

function assessmentHtml(report) {
  const fields = [
    ['overview', 'Overview'], ['architecture', 'Architecture'], ['features', 'Features'],
    ['codeQuality', 'Code quality'], ['security', 'Security'], ['performance', 'Performance'],
    ['dependencies', 'Dependencies'], ['recommendations', 'Recommendations'],
    ['criteria', 'Criteria'], ['evidence', 'Evidence'], ['unknowns', 'Unknowns']
  ];
  const items = fields.filter(([key]) => report && report[key] !== undefined && report[key] !== null && report[key] !== '');
  return `<section class="output-section">
    <h3>Evidence-based assessment</h3>
    <div class="assessment-list">
      ${items.length ? items.map(([key, label]) => `
        <article class="assessment-card">
          <span class="result-label">${escapeHtml(label)}</span>
          ${Array.isArray(report[key]) ? resultList(report[key]) : `<p>${escapeHtml(formatResultValue(report[key]))}</p>`}
        </article>`).join('') : '<div class="empty-result">No structured assessment was returned.</div>'}
    </div>
    <details class="raw-details">
      <summary>Developer details · raw structured response</summary>
      <pre>${escapeHtml(JSON.stringify(report || {}, null, 2))}</pre>
    </details>
  </section>`;
}

function humanizeKey(value) {
  return String(value)
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, char => char.toUpperCase());
}

function formatResultValue(value) {
  if (Array.isArray(value)) return value.map(item => formatResultValue(item)).join(' · ');
  if (value && typeof value === 'object') {
    return Object.entries(value).map(([key, item]) => `${humanizeKey(key)}: ${formatResultValue(item)}`).join(' · ');
  }
  return value === null || value === undefined || value === '' ? 'Not provided' : String(value);
}

function setLoading(show, message) {
  els.loading.hidden = !show;
  if (show) {
    els.empty.hidden = true;
    els.loadingMessage.textContent = message;
  } else {
    els.empty.hidden = !els.output.hidden;
  }
}

function showError(message) {
  els.errorMessage.textContent = message;
  els.error.hidden = false;
}

function hideError() { els.error.hidden = true; }

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#039;', '"': '&quot;' }[char]));
}
