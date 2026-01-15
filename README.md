# Peer-2-Peer-Chat-Application
This project implements a fully decentralized peer-to-peer chat system based on a logical ring topology. The system operates without any central server and supports dynamic peer discovery, crash fault tolerance, and globally ordered message delivery.

Peers discover each other using periodic UDP broadcast announcements and establish TCP connections only with their immediate neighbors in the ring. Failures are detected through heartbeat mechanisms, allowing the system to autonomously repair the ring and continue operation. Leader election is performed using the LeLann–Chang–Roberts algorithm, and the elected leader acts as a sequencer to ensure total order multicast, providing a consistent global chat history across all peers.

Background

This project was developed within the Master of Science (M.Sc.) program as part of the course Distributed Systems at the University of Stuttgart, under the supervision of Prof. Dr. Marco Aiello.

---
**Contributors:** @Madinaxn, @dery28
