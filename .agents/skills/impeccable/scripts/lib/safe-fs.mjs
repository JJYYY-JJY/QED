import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const NO_FOLLOW = fs.constants.O_NOFOLLOW || 0;
const DIRECTORY = fs.constants.O_DIRECTORY || 0;
const DIRECTORY_FLAGS = fs.constants.O_RDONLY | DIRECTORY | NO_FOLLOW;
const DESCRIPTOR_ROOT = process.platform === 'linux' ? '/proc/self/fd' : null;
const MAX_REMOVE_DEPTH = 64;
const MAX_REMOVE_PASSES = 32;

export function isPathInsideOrEqual(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === '' || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`));
}

export function resolveContainedPath(root, candidate, options = {}) {
  const { absoluteRoot, absolute, parts } = normalizeContainedPath(root, candidate, {
    allowRoot: options.allowRoot === true,
  });

  let current = absoluteRoot;
  for (let index = 0; index < parts.length; index += 1) {
    current = path.join(current, parts[index]);
    let stat;
    try {
      stat = fs.lstatSync(current);
    } catch (error) {
      if (error?.code === 'ENOENT' && options.allowMissing !== false) return absolute;
      throw error;
    }
    if (stat.isSymbolicLink()) {
      throw new Error(`Path contains a symbolic link: ${current}`);
    }
    const isLeaf = index === parts.length - 1;
    if (!isLeaf && !stat.isDirectory()) {
      throw new Error(`Path component is not a directory: ${current}`);
    }
    if (isLeaf && options.type === 'file' && !stat.isFile()) {
      throw new Error(`Path is not a regular file: ${current}`);
    }
    if (isLeaf && options.type === 'directory' && !stat.isDirectory()) {
      throw new Error(`Path is not a directory: ${current}`);
    }
  }
  return absolute;
}

export function ensureContainedDirectory(root, directory) {
  const pinned = pinDirectory(root, directory, { create: true });
  fs.closeSync(pinned.descriptor);
  return pinned.absolute;
}

export function makeContainedTemporaryDirectory(root, parent, prefix = 'tmp-') {
  if (typeof prefix !== 'string' || !/^[A-Za-z0-9._-]*$/.test(prefix)) {
    throw new Error('Invalid temporary directory prefix');
  }
  const pinned = pinDirectory(root, parent, { create: true });
  try {
    for (let attempt = 0; attempt < 16; attempt += 1) {
      const leaf = `${prefix}${randomUUID()}`;
      const target = descriptorEntryPath(pinned.descriptor, leaf);
      try {
        fs.mkdirSync(target, { mode: 0o700 });
        const descriptor = openDirectoryAt(
          pinned.descriptor,
          leaf,
          { create: false, displayPath: path.join(pinned.absolute, leaf) },
        );
        fs.closeSync(descriptor);
        return path.join(pinned.absolute, leaf);
      } catch (error) {
        if (error?.code !== 'EEXIST') throw error;
      }
    }
    throw new Error('Unable to allocate a contained temporary directory');
  } finally {
    fs.closeSync(pinned.descriptor);
  }
}

export function readContainedFile(root, file, encoding = null) {
  const pinned = pinParentDirectory(root, file, { create: false });
  let descriptor;
  try {
    descriptor = openRegularFileAt(
      pinned.descriptor,
      pinned.leaf,
      fs.constants.O_RDONLY | NO_FOLLOW,
      undefined,
      pinned.absolute,
    );
    return fs.readFileSync(descriptor, encoding === null ? undefined : { encoding });
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    fs.closeSync(pinned.descriptor);
  }
}

export function writeContainedFile(root, file, data, options = {}) {
  const pinned = pinParentDirectory(root, file, { create: true });
  const target = descriptorEntryPath(pinned.descriptor, pinned.leaf);
  let mode = options.mode ?? 0o600;
  let descriptor;
  let temporary;
  try {
    try {
      const stat = fs.lstatSync(target);
      if (stat.isSymbolicLink()) throw new Error(`Path contains a symbolic link: ${pinned.absolute}`);
      if (!stat.isFile()) throw new Error(`Path is not a regular file: ${pinned.absolute}`);
      mode = stat.mode & 0o777;
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
    }

    const temporaryLeaf = `.${pinned.leaf}.${process.pid}.${randomUUID()}.tmp`;
    temporary = descriptorEntryPath(pinned.descriptor, temporaryLeaf);
    descriptor = fs.openSync(
      temporary,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | NO_FOLLOW,
      mode,
    );
    assertRegularDescriptor(descriptor, pinned.absolute);
    fs.writeFileSync(descriptor, data, options.encoding ? { encoding: options.encoding } : undefined);
    fs.closeSync(descriptor);
    descriptor = undefined;
    fs.renameSync(temporary, target);
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    if (temporary) {
      try { fs.unlinkSync(temporary); } catch {}
    }
    fs.closeSync(pinned.descriptor);
  }
  return pinned.absolute;
}

export function appendContainedFile(root, file, data, options = {}) {
  const pinned = pinParentDirectory(root, file, { create: true });
  let descriptor;
  try {
    descriptor = openRegularFileAt(
      pinned.descriptor,
      pinned.leaf,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_APPEND | NO_FOLLOW,
      options.mode ?? 0o600,
      pinned.absolute,
    );
    fs.writeFileSync(descriptor, data, options.encoding ? { encoding: options.encoding } : undefined);
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    fs.closeSync(pinned.descriptor);
  }
  return pinned.absolute;
}

export function readContainedDirectory(root, directory, options = {}) {
  const pinned = pinDirectory(root, directory, { create: false });
  try {
    return fs.readdirSync(descriptorPath(pinned.descriptor), options);
  } finally {
    fs.closeSync(pinned.descriptor);
  }
}

export function removeContainedFile(root, file, options = {}) {
  let pinned;
  try {
    pinned = pinParentDirectory(root, file, { create: false });
  } catch (error) {
    if (options.force === true && error?.code === 'ENOENT') return false;
    throw error;
  }
  const target = descriptorEntryPath(pinned.descriptor, pinned.leaf);
  try {
    let stat;
    try {
      stat = fs.lstatSync(target);
    } catch (error) {
      if (options.force === true && error?.code === 'ENOENT') return false;
      throw error;
    }
    if (stat.isSymbolicLink()) throw new Error(`Path contains a symbolic link: ${pinned.absolute}`);
    if (!stat.isFile()) throw new Error(`Path is not a regular file: ${pinned.absolute}`);
    fs.unlinkSync(target);
    return true;
  } finally {
    fs.closeSync(pinned.descriptor);
  }
}

export function removeContainedDirectory(root, directory, options = {}) {
  let pinned;
  try {
    pinned = pinParentDirectory(root, directory, { create: false });
  } catch (error) {
    if (options.force === true && error?.code === 'ENOENT') return false;
    throw error;
  }
  const target = descriptorEntryPath(pinned.descriptor, pinned.leaf);
  let directoryDescriptor;
  try {
    try {
      directoryDescriptor = openDirectoryAt(
        pinned.descriptor,
        pinned.leaf,
        { create: false, displayPath: pinned.absolute },
      );
    } catch (error) {
      if (options.force === true && error?.code === 'ENOENT') return false;
      throw error;
    }
    if (options.recursive === false) {
      if (fs.readdirSync(descriptorPath(directoryDescriptor)).length > 0) return false;
    } else {
      clearDirectoryDescriptor(directoryDescriptor, 0);
    }
    fs.closeSync(directoryDescriptor);
    directoryDescriptor = undefined;
    fs.rmdirSync(target);
    return true;
  } finally {
    if (directoryDescriptor !== undefined) fs.closeSync(directoryDescriptor);
    fs.closeSync(pinned.descriptor);
  }
}

function normalizeContainedPath(root, candidate, { allowRoot = false } = {}) {
  const absoluteRoot = path.resolve(root);
  const absolute = path.isAbsolute(candidate)
    ? path.resolve(candidate)
    : path.resolve(absoluteRoot, candidate);
  if (!isPathInsideOrEqual(absoluteRoot, absolute)) {
    throw new Error(`Path escapes project root: ${candidate}`);
  }
  const relative = path.relative(absoluteRoot, absolute);
  if (!relative && !allowRoot) {
    throw new Error('Path must name an entry below the project root');
  }
  const parts = relative ? relative.split(path.sep) : [];
  if (parts.some((part) => !part || part === '.' || part === '..' || part.includes('\0'))) {
    throw new Error(`Invalid contained path: ${candidate}`);
  }
  return { absoluteRoot, absolute, parts };
}

function pinDirectory(root, directory, { create }) {
  requireDescriptorFilesystem();
  const normalized = normalizeContainedPath(root, directory, { allowRoot: true });
  let descriptor = openRootDirectory(normalized.absoluteRoot);
  let current = normalized.absoluteRoot;
  try {
    for (const part of normalized.parts) {
      current = path.join(current, part);
      const next = openDirectoryAt(descriptor, part, { create, displayPath: current });
      fs.closeSync(descriptor);
      descriptor = next;
    }
    return { descriptor, absolute: normalized.absolute };
  } catch (error) {
    fs.closeSync(descriptor);
    throw error;
  }
}

function pinParentDirectory(root, file, { create }) {
  requireDescriptorFilesystem();
  const normalized = normalizeContainedPath(root, file);
  const leaf = normalized.parts.at(-1);
  const parentParts = normalized.parts.slice(0, -1);
  let descriptor = openRootDirectory(normalized.absoluteRoot);
  let current = normalized.absoluteRoot;
  try {
    for (const part of parentParts) {
      current = path.join(current, part);
      const next = openDirectoryAt(descriptor, part, { create, displayPath: current });
      fs.closeSync(descriptor);
      descriptor = next;
    }
    return { descriptor, absolute: normalized.absolute, leaf };
  } catch (error) {
    fs.closeSync(descriptor);
    throw error;
  }
}

function openRootDirectory(root) {
  let descriptor;
  try {
    descriptor = fs.openSync(root, DIRECTORY_FLAGS);
    assertDirectoryDescriptor(descriptor, root);
    return descriptor;
  } catch (error) {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    throw pathTypeError(error, root, 'directory');
  }
}

function openDirectoryAt(parentDescriptor, name, { create, displayPath }) {
  const candidate = descriptorEntryPath(parentDescriptor, name);
  let descriptor;
  try {
    descriptor = fs.openSync(candidate, DIRECTORY_FLAGS);
  } catch (error) {
    if (create && error?.code === 'ENOENT') {
      try {
        fs.mkdirSync(candidate, { mode: 0o700 });
      } catch (mkdirError) {
        if (mkdirError?.code !== 'EEXIST') throw pathTypeError(mkdirError, displayPath, 'directory');
      }
      try {
        descriptor = fs.openSync(candidate, DIRECTORY_FLAGS);
      } catch (openError) {
        throw pathTypeError(openError, displayPath, 'directory');
      }
    } else {
      throw pathTypeError(error, displayPath, 'directory');
    }
  }
  try {
    assertDirectoryDescriptor(descriptor, displayPath);
    return descriptor;
  } catch (error) {
    fs.closeSync(descriptor);
    throw error;
  }
}

function openRegularFileAt(parentDescriptor, name, flags, mode, displayPath) {
  let descriptor;
  try {
    descriptor = mode === undefined
      ? fs.openSync(descriptorEntryPath(parentDescriptor, name), flags)
      : fs.openSync(descriptorEntryPath(parentDescriptor, name), flags, mode);
    assertRegularDescriptor(descriptor, displayPath);
    return descriptor;
  } catch (error) {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    throw pathTypeError(error, displayPath, 'file');
  }
}

function clearDirectoryDescriptor(descriptor, depth) {
  if (depth > MAX_REMOVE_DEPTH) throw new Error('Contained directory exceeds safe removal depth');
  const base = descriptorPath(descriptor);
  for (let pass = 0; pass < MAX_REMOVE_PASSES; pass += 1) {
    const names = fs.readdirSync(base);
    if (names.length === 0) return;
    for (const name of names) {
      const entryPath = descriptorEntryPath(descriptor, name);
      let childDescriptor;
      try {
        childDescriptor = fs.openSync(entryPath, DIRECTORY_FLAGS);
        assertDirectoryDescriptor(childDescriptor, entryPath);
      } catch {
        if (childDescriptor !== undefined) fs.closeSync(childDescriptor);
        try { fs.unlinkSync(entryPath); } catch (error) {
          if (!['ENOENT', 'EISDIR', 'EPERM'].includes(error?.code)) throw error;
        }
        continue;
      }
      try {
        clearDirectoryDescriptor(childDescriptor, depth + 1);
      } finally {
        fs.closeSync(childDescriptor);
      }
      try { fs.rmdirSync(entryPath); } catch (error) {
        if (!['ENOENT', 'ENOTDIR', 'ENOTEMPTY'].includes(error?.code)) throw error;
      }
    }
  }
  throw new Error('Contained directory changed during removal');
}

function assertDirectoryDescriptor(descriptor, displayPath) {
  if (!fs.fstatSync(descriptor).isDirectory()) {
    throw new Error(`Path is not a directory: ${displayPath}`);
  }
}

function assertRegularDescriptor(descriptor, displayPath) {
  if (!fs.fstatSync(descriptor).isFile()) {
    throw new Error(`Path is not a regular file: ${displayPath}`);
  }
}

function descriptorPath(descriptor) {
  requireDescriptorFilesystem();
  return `${DESCRIPTOR_ROOT}/${descriptor}`;
}

function descriptorEntryPath(descriptor, name) {
  if (typeof name !== 'string' || !name || name === '.' || name === '..' || name.includes('/') || name.includes('\0')) {
    throw new Error('Invalid descriptor-relative path component');
  }
  return `${descriptorPath(descriptor)}/${name}`;
}

function requireDescriptorFilesystem() {
  if (!DESCRIPTOR_ROOT) {
    throw new Error('Contained filesystem operations require Linux /proc/self/fd support');
  }
}

function pathTypeError(error, displayPath, expectedType) {
  if (['ELOOP', 'ENOTDIR'].includes(error?.code)) {
    return new Error(`Path contains a symbolic link or non-directory component: ${displayPath}`, { cause: error });
  }
  if (error?.code === 'EISDIR' || error?.code === 'EPERM') {
    return new Error(`Path is not a regular ${expectedType}: ${displayPath}`, { cause: error });
  }
  return error;
}
