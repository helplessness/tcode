const path = require('node:path');
process.argv.push('--test-instance');
require(path.join(require('./package.json').tcodeDesktopRoot, 'main.cjs'));
