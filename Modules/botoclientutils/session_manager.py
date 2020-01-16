import boto3
from threading import Lock, current_thread, Condition
from datetime import datetime, timedelta

# A custom implementation of a ReadWrite lock (many readers but only one writer)
class ReadWriteLock:
    def __init__(self):
        self.__read_ready = Condition(Lock())
        self.__readers = 0

    # __enter__ and __exit__ implement compatibility with the 'with' statement
    # Using this class with the 'with' statement is only possible for 'reader' as it uses the read lock.
    def __enter__(self):
        self.acquire_read()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release_read()
        return False

    # Acquire/Release read ; for readers
    def acquire_read(self):
        """ Acquire a read lock. Blocks only if a thread has
        acquired the write lock. """
        with self.__read_ready:
            self.__readers += 1

    def release_read(self):
        """ Release a read lock. """
        with self.__read_ready:
            self.__readers -= 1
            if self.__readers == 0:
                self.__read_ready.notifyAll()

    # Acquire/Release WRITE ; for writers
    def acquire_write(self):
        """ Acquire a write lock. Blocks until there are no
        acquired read or write locks. """
        self.__read_ready.acquire()
        while self.__readers > 0:
            self.__read_ready.wait()

    def release_write(self):
        """ Release a write lock. """
        self.__read_ready.release()

# Singleton for the STS client
def _get_sts_client():
    global g_sts_client
    if 'g_sts_client' not in globals():
        g_sts_client = boto3.client('sts')
    return g_sts_client

# Testing if STS credentials are near their expiration date
def _is_near_expiration(creds):
    if creds is None:
        return False
    return datetime.now(creds['Expiration'].tzinfo) + timedelta(seconds=30) > creds['Expiration']

# Testing if STS credentials are expired (used only by the cleanup method of SessionManager)
def _is_expired(creds):
    if creds is None:
        return False
    return datetime.now(creds['Expiration'].tzinfo) > creds['Expiration']

# The factory itself is a Singleton.
# If used to instanciate a SessionManager, it will always return the same SessionManager object when the same role/session_name are used.
class SessionManagerFactory():
    __instance = None

    def __new__(cls):
        if SessionManagerFactory.__instance is None:
            SessionManagerFactory.__instance = object.__new__(cls)
            SessionManagerFactory.__instance.__instances = {}
        return SessionManagerFactory.__instance
    def __call__(self, remote_role_name=None, role_session_name='default'):
        key = str(remote_role_name) + str(role_session_name)
        if key not in self.__instances:
            self.__instances[key] = SessionManager(remote_role_name, role_session_name)
        return self.__instances[key]

# SessionManager exposes only 3 public methods:
#    - get_client
#    - get_resource
#    - clean_expired
# All underlying complexity is masked
# The goal of the Class is to reduce the memory usage by instanciating a specific client/resource only once.
# Furthermore, the Class is thread-safe and thread-aware : different thread will receive different clients/resources.
# Thread may still share the same boto Session but the client/resource instanciation is protected by a Lock specific to the Session.
# This is because boto Session perform endpoint discovery when instenciating a client, which cannot be done simultaniously by multiple thread.
class SessionManager:
    # The default STS token duration is 15minutes
    sts_token_duration = 900

    def __init__(self, remote_role_name, role_session_name='default'):
        self.__remote_role_name = remote_role_name
        self.__role_session_name = role_session_name
        self.__credentials = {}
        self.__active_objects = {}
        self.__session_locks = {}
        self.__lock = Lock()
        self.__rwlock = ReadWriteLock()

    # Return the role ARN for an account, using the role_name passed at instanciation to __init__
    def __get_remote_role(self, account):
        return f'arn:aws:iam::{account}:role/{self.__remote_role_name}'

    # Return STS credentials for an account ; an STS call is done only if a token does not already exist or if it's expired
    def __get_credentials(self, account):
        cred_key = f'creds_{account}'
        if cred_key not in self.__credentials or _is_near_expiration(self.__credentials[cred_key]):
            assumed_role = _get_sts_client().assume_role(
                RoleArn = self.__get_remote_role(account),
                RoleSessionName = self.__role_session_name,
                DurationSeconds = SessionManager.sts_token_duration
            )
            self.__credentials[cred_key] = assumed_role['Credentials']
        return self.__credentials[cred_key]

    # Return the boto Session and the underlying creds for an account ;
    # a new Session is created only if the Session does not exist or if the creds are expired.
    # In order to avoid multiple Session instanciation for an account, Session instanciation is protected by a global Lock
    # One could want an account specific Lock, but in any case the STS call done by get_credentials must remain globally locked.
    def __get_session(self, account):
        session_key = f'session_{account}'
        if session_key not in self.__active_objects or _is_near_expiration(self.__active_objects[session_key]['creds']):
            with self.__lock:
                # The double-IF statement is NOT an mistake nor it is useless.
                # If one believe it is useless or a mistake, I can only advice to read more about multithreading and parallelism issues.
                # In two words, the first IF avoid locking if not needed, the second ensure we cannot have double instanciation
                if session_key not in self.__active_objects or _is_near_expiration(self.__active_objects[session_key]['creds']):
                    # If there is an account, retrieve credentials and create a new Session object
                    if account is not None:
                        creds = self.__get_credentials(account)
                        session = boto3.session.Session(
                            aws_access_key_id = creds['AccessKeyId'],
                            aws_secret_access_key = creds['SecretAccessKey'],
                            aws_session_token = creds['SessionToken'],
                        )
                        # The object we will return is a dictionary containing the Session and the Creds
                        # This is usefull for the cleanup method (clean_expired).
                        self.__active_objects[session_key] ={'creds':creds,'session':session}
                    # Else simply take the default session and set the creds to None
                    else:
                        self.__active_objects[session_key] = {'creds':None,'session':boto3.session.Session()}
        # Return the dictionary containing the Session and the Creds
        return self.__active_objects[session_key]

    # Return the Lock associated with a session (in fact, an account, but it's really the same). Must remain globally Locked
    def __get_session_lock(self, account):
        lock_key = f'lock_{account}'
        if lock_key not in self.__session_locks:
            with self.__lock:
                if lock_key not in self.__session_locks:
                    self.__session_locks[lock_key] = Lock()
        return self.__session_locks[lock_key]

    # Return the list of available regions for a service
    def get_available_regions(self, service_name):
        return self.__get_session(None)['session'].get_available_regions(service_name)

    # Get a client for the service (client_name), account (optional) and region (optional, defaults to eu-west-1)
    def get_client(self, client_name, account=None, region='eu-west-1'):
        # Compute the key of the object
        # It is dependant of the service/region/threadname/account
        key = f'c_{client_name}_{region}_{current_thread().name}_{account}'
        if key not in self.__active_objects or _is_near_expiration(self.__active_objects[key]['creds']):
            # Here we use the Read/Write Lock (read-mode) ; this prevent the cleanup (clean_expired) to run while we are renewing the client
            # If there is not lock, we could run into a race condition where a freshly intantiated client is erased by the cleanup before being returned which wouold trigger an exception
            # No need for a double if statement here : the key is thread-specific, we cannot have double-instanciation we are just protecting ourselves against the cleanup.
            # Note that it is impossible that the cleanup suppress an object if we do not enter in this block:
            # that would suppose that our thread could sleep for 30seconds between the IF and the RETURN ; which is not likely to happen.
            with self.__rwlock:
                # Retrieve the Session
                s_dict = self.__get_session(account)
                # Use the Session/Account specific Lock
                with self.__get_session_lock(account):
                    # Instantiate the client
                    client = s_dict['session'].client(client_name, region_name = region)
                # Store a dictionnary with both the client and the creds (for cleanup purpose (clean_expired method))
                self.__active_objects[key] = {'creds':s_dict['creds'], 'client':client}
        return self.__active_objects[key]['client']

    # Adding a "client" method, doing the same thing as get_client
    def client(self, client_name, account=None, region='eu-west-1'):
        return self.get_client(client_name, account, region)

    # Get a resource for the service (resource_name), account (optional) and region (optional, defaults to eu-west-1)
    def get_resource(self, resource_name, account=None, region='eu-west-1'):
        # Compute the key of the object
        # It is dependant of the service/region/threadname/account
        key = f'r_{resource_name}_{region}_{current_thread().name}_{account}'
        if key not in self.__active_objects or _is_near_expiration(self.__active_objects[key]['creds']):
            # Here we use the Read/Write Lock (read-mode) ; this prevent the cleanup (clean_expired) to run while we are renewing the resource
            # If there is not lock, we could run into a race condition where a freshly intantiated resource is erased by the cleanup before being returned which wouold trigger an exception
            # No need for a double if statement here : the key is thread-specific, we cannot have double-instanciation we are just protecting ourselves against the cleanup.
            # Note that it is impossible that the cleanup suppress an object if we do not enter in this block:
            # that would suppose that our thread could sleep for 30seconds between the IF and the RETURN ; which is not likely to happen.
            with self.__rwlock:
                # Retrieve the Session
                s_dict = self.__get_session(account)
                # Use the Session/Account specific Lock
                with self.__get_session_lock(account):
                    # Instantiate the resource
                    resource = s_dict['session'].resource(resource_name, region_name = region)
                # Store a dictionnary with both the resource and the creds (for cleanup purpose (clean_expired method))
                self.__active_objects[key] = {'creds':s_dict['creds'], 'resource':resource}
        return self.__active_objects[key]['resource']

    # Adding a "resource" method, doing the same thing as get_resource
    def resource(self, resource_name, account=None, region='eu-west-1'):
        return self.get_resource(resource_name, account, region)

    # When called, this method will remove all the expired client/resource/session
    # The purpose is to free the memory from unused client/resource/session
    # Note that the method is based on _is_expired wheras client/resource/session are based on _is_near_expiration
    # This induce the 30 seconds security that prevent the problem pointed out in get_client and get_resource
    def clean_expired(self):
        # We start by acquire the RWLock in write mode
        # We will wait for all the "readers" (in fact, thread that instanciate clients) to finish their job.
        # Once we get the lock, all the "readers" will wait that we finish our job
        self.__rwlock.acquire_write()
        try:
            # List all the keys to clean
            key_to_clean = [key for key in self.__active_objects if _is_expired(self.__active_objects[key]['creds'])]
            # Clean them
            for key in key_to_clean:
                del self.__active_objects[key]
            # List all the creds to clean
            cred_to_clean = [cred_key for cred_key in self.__credentials if _is_expired(self.__credentials[cred_key])]
            # Clean them
            for cred_key in cred_to_clean:
                del self.__credentials[cred_key]
        finally:
            # Release the Lock
            self.__rwlock.release_write()
