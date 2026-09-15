document.addEventListener('DOMContentLoaded', () => {
  const mode = document.getElementById('service_mode');
  if (!mode) return;
  const update = () => {
    const taxi = mode.value === 'taxi';
    for (const name of ['daily_rate', 'weekly_rate', 'deposit']) {
      const input = document.getElementById(name);
      if (!input) continue;
      input.disabled = taxi;
      input.required = !taxi && name !== 'weekly_rate';
      input.closest('.field').hidden = taxi;
    }
  };
  mode.addEventListener('change', update);
  update();
});
