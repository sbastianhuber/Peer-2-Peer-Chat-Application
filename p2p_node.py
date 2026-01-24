"""
Decentralized Peer-to-Peer Chat Application with Ring Topology

This module implements a fully decentralized P2P chat system using a logical ring
architecture. Key features include:

- Dynamic peer discovery via UDP broadcasts (no central server)
- Fault-tolerant ring topology with automatic repair
- Leader election using the Le-Lann-Chang-Roberts (LCR) algorithm
- Unidirectional message forwarding around the ring
- Message deduplication to prevent infinite loops

Architecture Overview:
- Each node connects to exactly one "right neighbor" forming a logical ring
- Messages flow unidirectionally around the ring until they return to origin
- UDP broadcasts enable decentralized peer discovery
- TCP connections provide reliable ring communication
- Leader election occurs automatically when topology changes or leader fails
"""

import socket #Testt
import threading
import time
import json
import uuid
import sys
import logging
import signal
from collections import deque # TEST 

# --- CONFIGURATION ---
BROADCAST_PORT = 50000        
TCP_PORT = 6000               
CHECK_INTERVAL = 1 #Check every second for dead peers        
PEER_TIMEOUT = 4  # A peer is considered dead if no heartbeat in 4 seconds            
BUF_SIZE = 4096 # 4KB buffer size 

# Logging Configuration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

class NodeState:
    """
    Thread-safe container for node state shared across multiple threads.

    Attributes:
        lock: Threading lock for synchronizing access to shared state
        peers: Dict mapping peer IDs to their info {id: {'ip', 'port', 'last_seen'}}
        leader_id: Current coordinator's ID, None if no leader elected yet
        running: Flag to control main event loops across all threads
        participating: True when this node is actively in an election process
        seen_messages: Circular buffer (deque) storing recent message IDs for deduplication
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.peers = {}  # {peer_id: {'ip': str, 'port': int, 'last_seen': timestamp}}
        self.leader_id = None # Current coordinator ID
        self.running = True # Global running flag for all threads
        self.participating = False # LCR election participation flag
        self.seen_messages = deque(maxlen=200)  # Stores last 200 message IDs 

class P2PNode:
    """
    Main P2P node implementing ring-based topology and LCR leader election.

    Each node maintains exactly one outgoing TCP connection to its "right neighbor"
    in the logical ring, while accepting connections from its "left neighbor".
    The ring is dynamically constructed by sorting all known peer IDs.

    Attributes:
        id: Full UUID string (used for display and election messages)
        id_int: Integer representation of UUID (used for LCR election comparisons)
        name: Human-readable display name
        port: TCP port for accepting ring connections
        ip: Local IP address on the network
        state: Thread-safe NodeState container
        udp_sock: Socket for UDP broadcast discovery
        tcp_server: Socket for accepting TCP connections from left neighbor
        right_neighbor_socket: Active TCP connection to right neighbor
        right_neighbor_info: Tuple (peer_id, ip, port) of current right neighbor
    """
    def __init__(self, name, port=TCP_PORT):
        # Generate unique node ID using UUID4
        # We maintain two forms: full string for messages, integer for election logic
        _uuid = uuid.uuid4()
        self.id = str(_uuid)  # Full UUID string for identification
        self.id_int = _uuid.int  # Integer UUID for LCR comparison (ensures total ordering)

        self.name = name
        self.port = int(port)
        self.ip = self._get_local_ip()  # Detect local network IP

        if self.ip.startswith("127."): # If Localhost detected, exit program
            logger.critical("No routable local IP address detected. " 
                            "Node cannot participate in P2P network."
            )
            sys.exit(1)
        
        self.state = NodeState()  # Thread-safe shared state

        # Network sockets (initialized later)
        self.udp_sock = None  # For broadcast discovery
        self.tcp_server = None  # For accepting connections
        self.right_neighbor_socket = None  # Connection to next node in ring
        self.right_neighbor_info = None  # (peer_id, ip, port) tuple

        logger.info(f"Node Initialized: {self.name} (ID: {self.id[:8]}...) @ {self.ip}:{self.port}")

    def _get_local_ip(self):
        """
        Detect the local IP address on the active network interface.

        Uses a clever trick: create a UDP socket and "connect" to a remote address
        (8.8.8.8, Google DNS). This doesn't send any packets but causes the OS
        to determine which local interface would be used, revealing our local IP.

        Returns:
            str: Local IP address (e.g., '192.168.1.5') or '127.0.0.1' on failure
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Doesn't actually send packets; just determines the route
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]  # Get local address from socket
        except Exception:
            ip = '127.0.0.1'  # Fallback to localhost if network unavailable
        finally:
            s.close()
        return ip

    def start(self):
        """
        Start the P2P node and all background services.

        Initializes all network services and threads in the following order:
        1. Register signal handlers for graceful shutdown
        2. Start UDP listener (receive peer announcements)
        3. Start UDP broadcaster (announce our presence)
        4. Start TCP server (accept connections from left neighbor)
        5. Start ring maintenance loop (cleanup dead peers, stabilize ring)
        6. Enter main input loop (blocks here until shutdown)
        """
        # Register signal handlers for Ctrl+C and termination signals
        signal.signal(signal.SIGINT, self.stop) # Ctrl+C
        signal.signal(signal.SIGTERM, self.stop) # regular stop through OS

        # Start all background services
        self._start_udp_listener()  # Discover other peers
        self._start_udp_broadcaster()  # Announce ourselves
        self._start_tcp_server()  # Accept ring connections
        threading.Thread(target=self._ring_maintenance_loop, daemon=True).start()

        # Enter main loop (blocks until shutdown)
        self._input_loop()

    def stop(self, signum=None, frame=None):
        """
        Gracefully shutdown the node and clean up all resources.

        Stops all background threads by setting the running flag to False,
        then closes all network sockets. Can be called directly or via signal handler.

        Args:
            signum: Signal number (if called from signal handler) e.g. ctrl +C
            frame: Current stack frame (if called from signal handler), not used here but required by signal module.
        """
        logger.info("Shutting down system...")
        self.state.running = False  # Stop all background loops

        # Close all sockets
        if self.udp_sock: self.udp_sock.close()
        if self.tcp_server: self.tcp_server.close()
        if self.right_neighbor_socket: self.right_neighbor_socket.close()

        sys.exit(0)

    # --- DISCOVERY ---
    def _start_udp_listener(self):
        """
        Start background thread listening for UDP broadcast HELLO messages.

        Listens on BROADCAST_PORT for peer announcements. When a HELLO message
        is received from another node, updates the peer list with their contact
        info and timestamp. This enables decentralized peer discovery without
        any central server.

        The listener runs in a daemon thread until self.state.running becomes False.
        """
        # Create UDP socket with broadcast capability
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # create udp socket with AF_INET: Adress family IPv4 and SOCK_DGRAM: Datagram Socket UDP
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1) # SOL_SOCKET --> general socket settings. SO_BROADCAST: 1 Activate broadcast Socket option
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow Port address reuse

        try:
            self.udp_sock.bind(('', BROADCAST_PORT)) # Enable listening on all interfaces on BROADCAST_PORT  '' ==  0.0.0.0 --> all Interfaces (Wlan, VPN, Lan, ...) 
        except OSError as e:
            logger.error(f"UDP Port {BROADCAST_PORT} occupied: {e}")
            sys.exit(1)

        def listen():
            """Inner function: continuously receive and process HELLO messages."""
            while self.state.running:
                try:
                    data, _ = self.udp_sock.recvfrom(BUF_SIZE) # ignoring sender address with _ because ip + port are included in the message. The OS selects the appropriate network interface wich could be different than the TCP Server interface.
                    msg = json.loads(data.decode())

                    # Ignore our own broadcasts and non-HELLO messages
                    if msg['type'] == 'HELLO' and msg['id'] != self.id:
                        # Thread-safe update of peer list
                        with self.state.lock:
                            self.state.peers[msg['id']] = {
                                'ip': msg['ip'],
                                'port': msg['port'],
                                'last_seen': time.time()  # Local timestamp for timeout detection
                            }
                except Exception:
                    pass  # Ignore malformed messages

        threading.Thread(target=listen, daemon=True).start() # Start a daemon thread that continuously listens for incoming UDP HELLO messages in the background

    def _start_udp_broadcaster(self):
        """
        Start background thread that periodically broadcasts HELLO messages.

        Every CHECK_INTERVAL seconds, broadcasts our node info (ID, IP, port)
        to the local network. This allows other nodes to discover us without
        any central coordination.

        The broadcaster runs in a daemon thread until self.state.running becomes False.
        """
        def broadcast():
            """Inner function: periodically send HELLO broadcasts."""
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # create udp socket with AF_INET: Adress family IPv4 and SOCK_DGRAM: Datagram Socket UDP
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1) # SOL_SOCKET --> general socket settings. SO_BROADCAST: 1 Activate broadcast Socket option

            while self.state.running:
                # Prepare HELLO message with our contact info
                msg = {"type": "HELLO", "id": self.id, "ip": self.ip, "port": self.port} # TCP Port for Ring Connections
                try:
                    # Send to broadcast address (reaches all nodes on the local network)
                    sock.sendto(json.dumps(msg).encode(), ('<broadcast>', BROADCAST_PORT))
                except OSError:
                    pass  # Ignore network errors (e.g., no network interface) because Discovery is best-effort

                time.sleep(CHECK_INTERVAL)  # Wait before next broadcast

        threading.Thread(target=broadcast, daemon=True).start() # Start a daemon thread that periodically broadcasts our HELLO message in the background

    # --- TCP & RING ---
    def _start_tcp_server(self):
        """
        Start TCP server to accept connections from our left neighbor in the ring.

        In the ring topology, each node connects TO its right neighbor and
        ACCEPTS FROM its left neighbor. This server handles incoming connections.

        The server runs in a daemon thread, spawning a new handler thread for
        each accepted connection (typically just one - our left neighbor).
        """
        # Create TCP server socket
        self.tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM) # create tcp socket with AF_INET: Adress family IPv4 and SOCK_STREAM: Stream Socket TCP
        self.tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # Allow Port address reuse
        self.tcp_server.bind((self.ip, self.port)) # Bind to our local IP and specified port
        self.tcp_server.listen(5)  # Allow a small queue of pending incoming connections during short connection races between established connections and acceptance or reconnections

        def accept_clients():
            """Inner function: accept incoming TCP connections."""
            while self.state.running:
                try:
                    client, addr = self.tcp_server.accept()
                    # Spawn handler thread for this connection
                    threading.Thread(target=self._handle_client_connection, args=(client,), daemon=True).start()
                except OSError:
                    break  # Socket closed during shutdown

        threading.Thread(target=accept_clients, daemon=True).start() # Start a daemon thread that accepts incoming TCP connections in the background
    
    def _handle_client_connection(self, sock):
        """
        Handle incoming TCP connection (typically from our left neighbor).

        Reads line-delimited JSON messages from the socket and processes them.
        Each message is a JSON object terminated by newline (\n).

        This implements the receive side of our ring communication protocol.
        Messages can be: HEARTBEAT, CHAT, ELECTION, or COORDINATOR.

        Args:
            sock: Connected TCP socket to read from
        """
        sock.settimeout(None)  # TCP connection should persist permanently
        f = sock.makefile('r', encoding='utf-8')  # Line-buffered text stream. Every JSON Message is delimited by newline. data = json.dumps(msg) + "\n" 

        try:
            # Read messages line by line
            for line in f: #file
                if not line: break  # Connection closed # End of File. 

                try:
                    msg = json.loads(line.strip())
                    self._process_message(msg)  # Handle the message
                except json.JSONDecodeError:
                    logger.warning("Received invalid JSON")
        except Exception:
            pass  # Connection error, socket will be closed below
        finally:
            sock.close()

    def _ring_maintenance_loop(self):
        """
        Background maintenance loop for ring health and stability.

        Runs every CHECK_INTERVAL seconds to:
        1. Remove dead peers (timeout detection)
        2. Stabilize ring connections (repair broken links, send heartbeats)

        This ensures the ring self-heals from failures and maintains connectivity.
        """
        while self.state.running:
            time.sleep(CHECK_INTERVAL)
            self._cleanup_dead_peers()  # Remove timed-out peers
            self._stabilize_ring()  # Maintain ring topology

    def _cleanup_dead_peers(self):
        """
        Remove peers that haven't been seen recently (timeout detection).

        Checks all known peers against PEER_TIMEOUT. If a peer's last_seen
        timestamp is too old, removes them from the peer list. If the dead
        peer was the current leader, triggers a new election.

        This is the primary failure detection mechanism for the system.
        """
        now = time.time()
        with self.state.lock:
            # Find all peers whose last HELLO was more than PEER_TIMEOUT seconds ago
            dead = [pid for pid, info in self.state.peers.items()
                    if now - info['last_seen'] > PEER_TIMEOUT]

            for pid in dead:
                logger.warning(f"Peer {pid} detected dead (Timeout).")
                del self.state.peers[pid]

                # Special case: if the leader died, trigger election
                if self.state.leader_id == pid:
                    logger.critical(f"LEADER {pid} FAILED! Initiating Election.")
                    self.state.leader_id = None
                    self.state.participating = False
                    # Start election in separate thread to avoid blocking
                    threading.Thread(target=self._start_election, daemon=True).start()

    def _stabilize_ring(self):
        """
        Maintain ring topology by connecting to the correct right neighbor.

        Ring construction algorithm:
        1. Sort all known node IDs (self + discovered peers) lexicographically
        2. Find our position in the sorted list
        3. Our right neighbor is the next node in the list (wrapping around)

        Handles three cases:
        - Isolation: we're the only node (do nothing)
        - Topology change: our successor changed (reconnect + trigger election)
        - Normal operation: connection stable (send heartbeat)
        """
        # Build sorted list of all node IDs (atomic snapshot with lock)
        with self.state.lock:
            all_ids = sorted([self.id] + list(self.state.peers.keys()))

        # Calculate our right neighbor using ring logic
        my_idx = all_ids.index(self.id)
        successor_id = all_ids[(my_idx + 1) % len(all_ids)]  # Modulo for wrap-around

        # Case 1: We're isolated (only node in the network)
        if successor_id == self.id:
            if self.right_neighbor_socket:
                logger.info("Network isolation detected. Closing connections.")
                self._close_right_neighbor()
            return

        # Get successor's contact info (with lock)
        with self.state.lock:
            if successor_id not in self.state.peers: return  # Race condition: peer just removed
            target = self.state.peers[successor_id]

        # Case 2: Topology changed - our successor is different now
        if self.right_neighbor_info and self.right_neighbor_info[0] != successor_id:
            logger.info(f"Topology Change: Switching to {successor_id}")
            self._connect_to_neighbor(successor_id, target['ip'], target['port'])
            self.state.participating = False
            self._start_election()  # Topology change triggers election

        # Case 3: No current connection - establish initial link
        elif self.right_neighbor_socket is None:
            logger.info(f"Connecting to right neighbor: {successor_id}")
            self._connect_to_neighbor(successor_id, target['ip'], target['port'])
            self.state.participating = False
            self._start_election()  # New connection triggers election

        # Case 4: Normal operation - connection stable, send heartbeat
        else:
            try:
                self._send_json({"type": "HEARTBEAT", "msg_id": str(uuid.uuid4())})
            except Exception:
                # Connection lost - will reconnect on next stabilize cycle
                self._close_right_neighbor()

    def _connect_to_neighbor(self, pid, ip, port):
        """
        Establish TCP connection to our right neighbor in the ring.

        Closes any existing connection first, then attempts to connect to
        the specified peer. Uses a 5-second timeout for connection attempt.

        Args:
            pid: Peer ID of the target neighbor
            ip: IP address to connect to
            port: TCP port to connect to
        """
        self._close_right_neighbor()  # Close old connection if any

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)  # 5-second timeout for connection attempt
            sock.connect((ip, port))
            sock.settimeout(None)  # Back to blocking mode for normal operation

            # Store connection info
            self.right_neighbor_socket = sock
            self.right_neighbor_info = (pid, ip, port)
        except Exception as e:
            logger.error(f"Failed to connect to {ip}:{port}: {e}")

    def _close_right_neighbor(self):
        """
        Close connection to right neighbor and clear connection info.

        Safe to call even if no connection exists. Ignores any errors during close.
        """
        if self.right_neighbor_socket:
            try:
                self.right_neighbor_socket.close()
            except:
                pass  # Ignore errors during close

            self.right_neighbor_socket = None
            self.right_neighbor_info = None

    def _send_json(self, msg):
        """
        Send a JSON message to our right neighbor in the ring.

        Implements the send side of our line-delimited JSON protocol.
        Each message is serialized to JSON and terminated with newline.

        Args:
            msg: Dictionary to send (will be JSON-serialized)

        Raises:
            OSError: If the send fails (connection lost)
        """
        if not self.right_neighbor_socket: return  # No connection

        try:
            data = json.dumps(msg) + "\n"  # Line-delimited JSON
            self.right_neighbor_socket.sendall(data.encode('utf-8'))
        except OSError:
            raise  # Propagate error to caller for handling

    # --- MESSAGE PROCESSING (FIXED) ---
    def _process_message(self, msg):
        """
        Process incoming messages from ring neighbors.

        Implements three-phase message handling:
        1. LCR Victory Check: Must happen BEFORE deduplication (see below)
        2. Message Deduplication: Prevent infinite loops in ring
        3. Message Type Processing: Handle HEARTBEAT, CHAT, ELECTION, COORDINATOR

        The ordering is critical: victory detection must precede deduplication
        because when our election message completes the circuit and returns to us,
        we need to recognize it as victory BEFORE marking it as duplicate.

        Args:
            msg: Dictionary containing message data with 'type' field
        """
        mtype = msg.get('type')
        msg_id = msg.get('msg_id')

        if not msg_id: 
            logger.warning("Received message without msg_id, -dropping")
            return  # Ignore messages without msg_id

        # ===== PHASE 1: LCR VICTORY CHECK (MUST HAPPEN BEFORE DEDUP) =====
        # When an ELECTION message completes the ring and returns to its sender,
        # that node has won the election. We must detect this BEFORE marking
        # the message as seen, otherwise we'd incorrectly drop it as duplicate.
        if mtype == 'ELECTION' and str(msg.get('candidate_id')) == str(self.id):
            logger.info(">>> ELECTION WON! I am the new Coordinator. <<<")
            self.state.leader_id = self.id
            self.state.participating = False
            self._announce_coordinator()
            return  # Don't forward victory message

        # ===== PHASE 2: DEDUPLICATION =====
        # Messages circulate around the ring. To prevent infinite loops,
        # we track recently seen message IDs in a circular buffer.
        with self.state.lock:
            if msg_id in self.state.seen_messages:
                return  # Already processed this message, drop it
            self.state.seen_messages.append(msg_id)

        # ===== PHASE 3: MESSAGE TYPE PROCESSING =====
        if mtype == 'HEARTBEAT':
            # Just a keepalive, no action needed
            return

        elif mtype == 'CHAT':
            # Display chat message
            print(f"\r[CHAT] {msg['sender']}: {msg['text']}\n> ", end="")

            # Forward to next node unless we're the origin (message completed circuit)
            if msg['origin_id'] != self.id:
                self._forward(msg)

        elif mtype == 'ELECTION':
            # LCR Algorithm: Compare candidate's ID with ours
            candidate_val = msg['candidate_val']

            if candidate_val > self.id_int:
                # Candidate has higher ID - they're stronger, forward their message
                self.state.participating = True
                self._forward(msg)

            elif candidate_val < self.id_int:
                # Candidate has lower ID - we're stronger, replace with our ID
                # This is the core of LCR: suppress weaker candidates
                self._start_election()

            # Note: equality case (victory) was already handled in Phase 1

        elif mtype == 'COORDINATOR':
            # New leader announcement
            leader = msg['leader_id']
            self.state.leader_id = leader
            self.state.participating = False

            if leader != self.id:
                logger.info(f"New Coordinator confirmed: {leader}")
                self._forward(msg)  # Propagate announcement around ring 

    def _forward(self, msg):
        """
        Forward a message to our right neighbor in the ring.

        This is the core of ring-based message circulation. All messages
        (CHAT, ELECTION, COORDINATOR) flow unidirectionally around the ring
        until they return to their origin.

        Silently ignores errors - if forwarding fails, the message is dropped
        but the system continues operating. The ring will be repaired on the
        next maintenance cycle.

        Args:
            msg: Dictionary containing the message to forward
        """
        try:
            self._send_json(msg)
        except Exception:
            pass  # Ignore forwarding errors (connection will be repaired)

    def _start_election(self):
        """
        Initiate a leader election by sending our ID around the ring.

        This is called when:
        - Current leader fails (timeout)
        - Ring topology changes (peer joins/leaves)
        - We see an election message with a smaller candidate ID (LCR algorithm)

        Our message will circulate around the ring. If it returns to us unchanged,
        we've won the election (no node had a higher ID).

        Pre-adds our own election message to seen_messages to prevent processing
        it again if it comes back (victory is detected separately in _process_message).
        """
        if self.right_neighbor_socket:
            self.state.participating = True

            # Generate unique message ID and pre-add to seen list
            # This prevents re-processing if the message returns (victory case)
            e_msg_id = str(uuid.uuid4())
            with self.state.lock:
                self.state.seen_messages.append(e_msg_id)

            logger.info("Sending Election Message (My ID)...")
            self._forward({
                "type": "ELECTION",
                "candidate_id": self.id,  # String ID for display
                "candidate_val": self.id_int,  # Integer ID for comparison
                "msg_id": e_msg_id
            })

    def _announce_coordinator(self):
        """
        Announce ourselves as the new leader to all nodes in the ring.

        Called after winning an election. The COORDINATOR message circulates
        once around the ring, informing all nodes of the new leader.

        Pre-adds the announcement to seen_messages to prevent re-processing
        when it returns to us after completing the circuit.
        """
        c_msg_id = str(uuid.uuid4())
        with self.state.lock:
            self.state.seen_messages.append(c_msg_id)

        self._forward({
            "type": "COORDINATOR",
            "leader_id": self.id,
            "msg_id": c_msg_id
        })

    # --- UI ---
    def _input_loop(self):
        """
        Main user interface loop - handles user input and commands.

        Runs in the main thread, blocking on input(). Supports:
        - Regular text: Send as chat message to all peers via ring
        - /status: Display current leader, peer count, right neighbor
        - /quit: Gracefully shutdown the node

        This loop runs until the user quits or the program is interrupted.
        """
        print("\n--- DECENTRALIZED CHAT SYSTEM READY ---")
        print(f"My ID: {self.id}")

        while self.state.running:
            try:
                text = input("> ")
                if text.strip() == "": continue  # Ignore empty input

                # Command: Display node status
                if text == "/status":
                    with self.state.lock:
                        cnt = len(self.state.peers)
                    neigh = self.right_neighbor_info[1] if self.right_neighbor_info else "None"
                    leader = self.state.leader_id if self.state.leader_id else "None"
                    print(f"--- STATUS ---\nLeader: {leader}\nPeers: {cnt}\nRight Neighbor: {neigh}\n--------------")

                # Command: Quit
                elif text == "/quit":
                    self.stop()

                # Regular message: Send chat to all peers
                else:
                    # Generate message ID and pre-add to seen list
                    # This prevents us from displaying our own message when it returns
                    msg_id = str(uuid.uuid4())
                    with self.state.lock:
                        self.state.seen_messages.append(msg_id)

                    msg = {
                        "type": "CHAT",
                        "origin_id": self.id,  # To detect when message completes circuit
                        "sender": self.name,  # Display name
                        "text": text,
                        "msg_id": msg_id
                    }
                    self._forward(msg)  # Send into the ring

            except (EOFError, KeyboardInterrupt):
                self.stop()  # Graceful shutdown on Ctrl+C or Ctrl+D

if __name__ == "__main__":
    """
    Entry point: Parse command-line arguments and start the P2P node.

    Usage: python p2p_node.py <NAME> [PORT]
      NAME: Human-readable display name for this node
      PORT: Optional TCP port (defaults to 6000)

    Example:
      python p2p_node.py Alice 6000
      python p2p_node.py Bob 6001
      python p2p_node.py Charlie 6002
    """
    if len(sys.argv) < 2:
        print("Usage: python p2p_node.py <NAME> [PORT]")
        sys.exit(1)

    name = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else TCP_PORT

    # Create and start the node
    node = P2PNode(name, port)
    node.start()  # Blocks here until shutdown
