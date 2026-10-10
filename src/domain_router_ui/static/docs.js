const toc = document.getElementById('docs-toc');
document.querySelectorAll('.docs-article h2').forEach((heading, index) => {
  heading.id = `section-${index + 1}`;
  const link = document.createElement('a');
  link.href = `#${heading.id}`;
  link.textContent = heading.textContent;
  toc.append(link);
});
