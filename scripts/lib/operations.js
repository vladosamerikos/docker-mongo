// ES5 keeps this script compatible with both mongo and mongosh.
function checked(database, command) {
    var response = database.runCommand(command);
    if (response.ok !== 1 && response.ok !== true) {
        throw new Error(response.errmsg || 'MongoDB command failed');
    }
    return response;
}

function databaseNames() {
    return checked(admin, {listDatabases: 1}).databases.map(function (item) {
        return item.name;
    });
}

function executeOperation(request) {
    var name = request.database;
    var target = name ? connection.getDB(name) : null;
    var user = request.username;
    var result;
    if (request.action === 'status') {
        return {version: checked(admin, {buildInfo: 1}).version};
    }
    if (request.action === 'list-dbs') {
        return {databases: databaseNames().filter(function (item) {
            return request.all || ['admin', 'local', 'config'].indexOf(item) === -1;
        })};
    }
    if (request.action === 'list-users') {
        // MongoDB 3.x lacks forAllDBs; project only public user metadata.
        result = admin.getCollection('system.users').find({}, {user: 1, db: 1, roles: 1}).toArray();
        return {users: result.filter(function (item) {
            return !name || item.db === name;
        }).map(function (item) {
            return {username: item.user, database: item.db, roles: item.roles};
        })};
    }
    if (request.action === 'create-db' || request.action === 'create-user') {
        var exists = databaseNames().indexOf(name) !== -1;
        if (request.action === 'create-db' && exists) {
            throw new Error('Database already exists: ' + name);
        }
        if (user) {
            if (checked(target, {usersInfo: user}).users.length) {
                throw new Error('User already exists in this database: ' + user);
            }
            checked(target, {createUser: user, pwd: request.password, roles: request.roles});
        }
        // Keeping an empty collection makes a new database visible.
        if (!exists) {
            try {
                checked(target, {create: '__mongo_manager'});
            } catch (error) {
                if (user) {
                    checked(target, {dropUser: user});
                }
                throw error;
            }
        }
        return {database: name, database_created: !exists, username: user || null,
            user_created: !!user, roles: request.roles || []};
    }
    if (request.action === 'delete-user') {
        checked(target, {dropUser: user});
        return {database: name, username: user, user_deleted: true};
    }
    if (request.action === 'delete-db') {
        if (databaseNames().indexOf(name) === -1) {
            throw new Error('Database does not exist: ' + name);
        }
        var users = checked(target, {usersInfo: 1}).users;
        if (users.length && !request.delete_users) {
            throw new Error('Database still has users. Delete them first or pass --delete-users.');
        }
        var removed = 0;
        if (request.delete_users) {
            users.forEach(function (item) {
                checked(target, {dropUser: item.user});
                removed += 1;
            });
        }
        checked(target, {dropDatabase: 1});
        return {database: name, database_deleted: true, users_deleted: removed};
    }
    throw new Error('Unknown operation: ' + request.action);
}

try {
    var connection = new Mongo('127.0.0.1:27017');
    var admin = connection.getDB('admin');
    var authentication = admin.auth(rootCredentials.username, rootCredentials.password);
    if (authentication !== 1 && authentication !== true && !(authentication && authentication.ok === 1)) {
        throw new Error('Root authentication failed');
    }
    var operationResult = executeOperation(request);
    print('__MONGO_JSON__' + JSON.stringify({ok: true, result: operationResult}));
} catch (error) {
    print('__MONGO_JSON__' + JSON.stringify({ok: false, error: String(error.message || error)}));
    quit(1);
}
