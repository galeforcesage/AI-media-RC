/**
 * a2ui.js — local deterministic renderer for the trusted LLM Remote catalog.
 *
 * This renderer intentionally supports only server-validated components. It
 * does not execute HTML, scripts, URLs, tool names, or arbitrary payloads.
 */
const A2UI = (() => {
  const PROTOCOL = 'a2ui-v0.9.1';
  const CATALOG = 'urn:llm-remote:a2ui:catalog:v1';
  const COMPONENTS = new Set(['RecordingList', 'ConfirmationCard', 'ErrorNotice']);

  function render(message, container) {
    if (!isValid(message) || !container) return null;
    const surface = document.createElement('section');
    surface.className = 'a2ui-surface message assistant';
    surface.dataset.surfaceId = message.surface_id || '';
    surface.setAttribute('aria-label', message.title || 'Dynamic result');

    if (message.title) {
      const heading = document.createElement('h3');
      heading.className = 'a2ui-title';
      heading.textContent = message.title;
      surface.appendChild(heading);
    }

    message.components.forEach((component) => {
      const node = renderComponent(component);
      if (node) surface.appendChild(node);
    });
    container.appendChild(surface);
    container.scrollTop = container.scrollHeight;
    return surface;
  }

  function isValid(message) {
    return !!message &&
      message.protocol === PROTOCOL &&
      message.catalog_id === CATALOG &&
      message.type === 'surface' &&
      Array.isArray(message.components) &&
      message.components.length <= 100 &&
      message.components.every((item) => COMPONENTS.has(item.component));
  }

  function renderComponent(component) {
    if (component.component === 'RecordingList') {
      return renderRecordingList(component.props || {});
    }
    if (component.component === 'ConfirmationCard') {
      return renderConfirmation(component.props || {}, component.actions || []);
    }
    if (component.component === 'ErrorNotice') {
      const notice = document.createElement('div');
      notice.className = 'a2ui-error';
      notice.textContent = String(component.props?.message || 'An error occurred.');
      return notice;
    }
    return null;
  }

  function renderRecordingList(props) {
    const list = document.createElement('div');
    list.className = 'a2ui-recording-list';
    const items = Array.isArray(props.items) ? props.items : [];
    items.forEach((item) => {
      const card = document.createElement('article');
      card.className = 'a2ui-recording-card';

      const title = document.createElement('div');
      title.className = 'a2ui-recording-title';
      title.textContent = item.title || 'Untitled recording';
      card.appendChild(title);

      if (item.episode_title) {
        const episode = document.createElement('div');
        episode.className = 'a2ui-recording-episode';
        episode.textContent = item.episode_title;
        card.appendChild(episode);
      }

      const facts = [
        item.season_episode,
        item.air_date,
        item.channel,
        watchedLabel(item.watched),
        item.status && item.status !== 'available' ? item.status : '',
      ].filter(Boolean);
      if (facts.length) {
        const meta = document.createElement('div');
        meta.className = 'a2ui-recording-meta';
        meta.textContent = facts.join(' · ');
        card.appendChild(meta);
      }
      list.appendChild(card);
    });
    return list;
  }

  function watchedLabel(value) {
    if (value === true || value === 1 || value === 'true') return 'Watched';
    if (value === false || value === 0 || value === 'false') return 'Unwatched';
    return '';
  }

  function renderConfirmation(props, actions) {
    const card = document.createElement('div');
    card.className = 'a2ui-confirmation';

    const message = document.createElement('p');
    message.textContent = props.message || 'Confirm this action?';
    card.appendChild(message);

    if (props.operation) {
      const operation = document.createElement('code');
      operation.textContent = props.operation;
      card.appendChild(operation);
    }

    const controls = document.createElement('div');
    controls.className = 'a2ui-actions';
    actions.forEach((action) => {
      if (!['confirm', 'cancel'].includes(action.action)) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `a2ui-action ${action.style || 'default'}`;
      button.textContent = action.label || action.action;
      button.addEventListener('click', async () => {
        controls.querySelectorAll('button').forEach((item) => { item.disabled = true; });
        try {
          const result = await API.dispatchUIAction(action.action_id, action.action);
          const outcome = document.createElement('div');
          outcome.className = result.error ? 'a2ui-error' : 'a2ui-outcome';
          outcome.textContent = result.response || result.error || 'Action completed.';
          card.appendChild(outcome);
          if (result.error) {
            controls.querySelectorAll('button').forEach((item) => { item.disabled = false; });
          } else {
            controls.remove();
          }
        } catch (error) {
          const outcome = document.createElement('div');
          outcome.className = 'a2ui-error';
          outcome.textContent = error.message || 'Action failed.';
          card.appendChild(outcome);
          controls.querySelectorAll('button').forEach((item) => { item.disabled = false; });
        }
      });
      controls.appendChild(button);
    });
    card.appendChild(controls);
    return card;
  }

  return { render };
})();
